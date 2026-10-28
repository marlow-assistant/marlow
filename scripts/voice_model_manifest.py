#!/usr/bin/env python3
"""Writes ios/Marlow/Marlow/VoiceModels.json: every file the iOS app downloads
for voice mode's Phonon voice, pinned to a Hugging Face commit, with its size.

The app never asks Hugging Face what to fetch. It downloads exactly this list
and checks each file's size, so a moved or rewritten repo fails the download
instead of silently changing the voice (docs/tasks/voice-mode-tts-engines.md).

Run by hand when the model is bumped:  python3 scripts/voice_model_manifest.py
"""

import json
import pathlib
import urllib.parse
import urllib.request

OUT = pathlib.Path(__file__).resolve().parent.parent / "ios/Marlow/Marlow/VoiceModels.json"

# FluidAudio's Core ML pack for Phonon 31m, at one commit of the repo. The
# files land in the app's folder laid out as under the pack's subdirectory,
# so the folder is the pack FluidAudio loads.
COREML = ("FluidInference/pocket-tts-coreml", "b51c4854305532b182026259092cc53e4b600f49")
PHONON_PACK = "phonon/phonon-31m-en"

# The voices the app offers (NeuralVoice.phonon in VoiceEngine.swift): the
# English catalogue voices and the checkpoint's default. Each is one
# .safetensors in the pack, and only these are downloaded.
PHONON_VOICES = (
    ["default"]
    + [f"en_gb_{n}" for n in [
        "Alfie", "Bryony", "Edwin", "Elodie-Rose", "Freya", "Hugh", "Imogen", "Lottie", "Maeve",
        "Pippa", "Spencer", "Teddy", "Toby"]]
    + [f"en_us_{n}" for n in [
        "Delia", "Easton", "Everly", "Hadley", "Harper", "Juniper", "Micah", "Miles", "Preston",
        "Russell", "Trevor", "Zoey"]]
)


def tree(repo, rev, folder=""):
    url = f"https://huggingface.co/api/models/{repo}/tree/{rev}/{urllib.parse.quote(folder)}?recursive=true"
    out, cursor = [], url
    while cursor:
        with urllib.request.urlopen(cursor) as r:
            out += json.load(r)
            # The tree API pages long listings with a Link header.
            link = r.headers.get("Link", "")
            cursor = link.split(";")[0].strip("<> ") if 'rel="next"' in link else None
    return {x["path"]: x["size"] for x in out if x["type"] == "file"}


def entry(repo, rev, path, size, local=None):
    return {
        "url": f"https://huggingface.co/{repo}/resolve/{rev}/{urllib.parse.quote(path)}",
        "path": local or path,
        "size": size,
    }


def coreml_pack(folder, voices):
    """What FluidAudio's own `PocketTtsSmallDownloader` keeps of a pack: the
    MLState package, the fp32 Mimi decoder, the JSON descriptors,
    `constants_bin/` and `voices/`. Not the fp32 reference models, the
    .mlpackage sources or `constants/`. Of the voice files, only `voices`."""
    files = tree(*COREML, folder)
    picked, found = [], set()
    for path, size in sorted(files.items()):
        rel = path[len(folder) + 1:]
        parts = rel.split("/")
        bundle = next((p for p in parts if p.endswith(".mlmodelc")), None)
        if any(p.endswith(".mlpackage") for p in parts) or parts[0] == "constants":
            continue
        if bundle and bundle not in ("pocket_state.mlmodelc", "mimi_decoder.mlmodelc"):
            continue
        if parts[-1] in ("manifest.json", ".DS_Store") or parts[-1].endswith(".wav"):
            continue
        if rel.endswith(".safetensors"):
            name = parts[-1][: -len(".safetensors")]
            if name not in voices:
                continue
            found.add(name)
        picked.append(entry(*COREML, path, size, local=rel))
    missing = set(voices) - found
    if missing:
        raise SystemExit(f"{folder}: no voice file for {sorted(missing)}")
    return picked


def main():
    manifest = {"phonon": {"files": coreml_pack(PHONON_PACK, PHONON_VOICES)}}
    OUT.write_text(json.dumps(manifest, indent=1) + "\n")
    for name, m in manifest.items():
        print(name, len(m["files"]), "files", round(sum(f["size"] for f in m["files"]) / 1e6), "MB")


if __name__ == "__main__":
    main()

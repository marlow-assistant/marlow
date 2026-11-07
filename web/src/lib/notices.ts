/** Settings › About › Open-source notices: what the web app ships, which is
 * the production set in `package-lock.json` plus Phonon's model. The texts are
 * the packages' own licence files in `src/notices/`, not typed here, and load
 * only when opened: phonon-tts alone is 200 KB (docs/tasks/settings-overhaul.md). */
export interface Notice {
  name: string;
  license: string;
  url: string;
  /** The text's name in `src/notices/`, without `.txt`. */
  file: string;
  /** The lockfile names this covers; `SettingsPanes.test.tsx` holds every
   * production package to having one. */
  packages: string[];
}

export const NOTICES: Notice[] = [
  { name: "React", license: "MIT", url: "https://react.dev", file: "react", packages: ["react", "react-dom", "scheduler"] },
  { name: "marked", license: "MIT", url: "https://github.com/markedjs/marked", file: "marked", packages: ["marked"] },
  { name: "jsQR", license: "Apache-2.0", url: "https://github.com/cozmo/jsQR", file: "jsqr", packages: ["jsqr"] },
  {
    name: "phonon-tts",
    license: "MIT or Apache-2.0",
    url: "https://www.npmjs.com/package/phonon-tts",
    file: "phonon-tts",
    packages: ["phonon-tts"],
  },
  {
    name: "Phonon 31m by Gradium",
    license: "Apache-2.0",
    url: "https://huggingface.co/gradium/phonon-31m-en",
    file: "apache-2.0",
    packages: [],
  },
];

const texts = import.meta.glob<string>("../notices/*.txt", { query: "?raw", import: "default" });

export function noticeText(notice: Notice): Promise<string> {
  return texts[`../notices/${notice.file}.txt`]();
}

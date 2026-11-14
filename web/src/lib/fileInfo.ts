// Display helpers for a file chip. None of this decides anything: the icon
// comes from the name's extension and is only a picture, never a filter on
// what may be attached (docs/tasks/share-and-files.md, "The type rule").

export type FileKind = "pdf" | "document" | "sheet" | "slides" | "text" | "archive" | "audio" | "video" | "image" | "other";

const KINDS: Record<FileKind, string[]> = {
  pdf: ["pdf"],
  document: ["doc", "docx", "dotx", "odt", "rtf", "pages"],
  sheet: ["xls", "xlsx", "xlsm", "ods", "csv", "tsv", "numbers"],
  slides: ["ppt", "pptx", "ppsx", "odp", "key"],
  text: ["txt", "md", "markdown", "log", "json", "yaml", "yml", "xml", "html", "htm", "ics", "vcf", "eml"],
  archive: ["zip", "tar", "gz", "tgz", "7z", "rar", "bz2"],
  audio: ["mp3", "m4a", "wav", "aac", "flac", "ogg", "opus"],
  video: ["mp4", "mov", "m4v", "avi", "mkv", "webm"],
  image: ["heic", "heif", "tif", "tiff", "bmp", "svg", "raw", "dng", "jpg", "jpeg", "png", "gif", "webp"],
  other: [],
};

const BY_EXTENSION = new Map<string, FileKind>(
  (Object.entries(KINDS) as [FileKind, string[]][]).flatMap(([kind, exts]) => exts.map((e) => [e, kind] as const)),
);

export function fileKind(name: string): FileKind {
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "other";
  return BY_EXTENSION.get(name.slice(dot + 1).toLowerCase()) ?? "other";
}

/** Keeps the start and the end, where the extension is, and puts the cut in
 * the middle. At most `max` characters. */
export function truncateMiddle(name: string, max: number): string {
  if (name.length <= max) return name;
  const keep = Math.max(1, max - 1);
  const tail = Math.ceil(keep / 2);
  const head = keep - tail;
  return `${name.slice(0, head)}…${name.slice(name.length - tail)}`;
}

/** "1.5 KB", "20 MB"; empty for a row from before sizes were recorded. */
export function formatSize(bytes: number | null | undefined): string {
  if (bytes == null) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  // One decimal under 10, none above: "2.4 MB", "250 KB".
  const text = value < 10 ? String(Math.round(value * 10) / 10) : String(Math.round(value));
  return `${text} ${units[unit]}`;
}

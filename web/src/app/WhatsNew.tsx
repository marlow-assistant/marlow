import type { Face } from "../lib/faces";
import { categoryLabel, changeDateText, isAlwaysTell, type ChangeEntry } from "../lib/whatsNew";
import { FaceImage } from "./Face";

/** Port of iOS `WhatsNewSheet` (docs/tasks/whats-new.md): "Marlow was
 * updated", once per update, with a hint at the Settings switch. Chat.tsx never shows it over
 * the approval card or another sheet. */
export function WhatsNewCard({
  entries,
  face,
  onDone,
}: {
  entries: ChangeEntry[];
  face: Face;
  onDone: () => void;
}) {
  return (
    <div className="modal-backdrop">
      <div className="modal whats-new" role="dialog" aria-modal="true" aria-labelledby="whats-new-title">
        <header className="whats-new-header">
          <span className="whats-new-face">
            <FaceImage face={face} size={44} crop="header" />
          </span>
          <div>
            <h2 id="whats-new-title">Marlow was updated</h2>
            <p className="whats-new-sub">Here's what changed.</p>
          </div>
        </header>
        <ul className="whats-new-list">
          {entries.map((e) => (
            <ChangeRow key={e.id} entry={e} />
          ))}
        </ul>
        <div className="whats-new-buttons">
          <button type="button" className="primary" onClick={onDone}>
            OK
          </button>
          {/* Not a button: "Don't show this again" read as hiding this one
              change. The switch lives in Settings. */}
          <p className="whats-new-hint">You can turn off What's new in Settings.</p>
        </div>
      </div>
    </div>
  );
}

export function ChangeRow({ entry }: { entry: ChangeEntry }) {
  return (
    <li className="whats-new-row">
      <span className="whats-new-meta">
        <span className={isAlwaysTell(entry.category) ? "whats-new-tell" : undefined}>{categoryLabel(entry.category)}</span>
        {" · "}
        {changeDateText(entry.date)}
      </span>
      <span className="whats-new-text">{entry.text}</span>
    </li>
  );
}

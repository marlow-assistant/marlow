import { memo } from "react";
import { describesUnseenWork, THINKING, WRITING } from "../lib/activity";
import type { ChatStore } from "../lib/chatStore";
import { segments } from "../lib/partialText";
import { attachmentIdsOf, imageKeysOf, type ChatMessage } from "../lib/types";
import { Markdown, SettledMarkdown } from "../markdown/Markdown";
import { Copyable } from "./CopyMenu";
import { useImageUrl, useStream } from "./hooks";

export interface RowProps {
  message: ChatMessage;
  store: ChatStore;
  divider?: string;
  /** SND-8: this user message's failed reply, when both are rendered. */
  failedReply?: ChatMessage;
  /** SND-8: this error placeholder is shown under its user bubble instead. */
  pairedAway?: boolean;
  isNew: boolean;
  onImageLoad: () => void;
}

/** One transcript row. The divider is part of the row (SCR-13: measure the
 * thing you scroll to), and the row element carries `data-id` so the
 * follow-scroll can find it. Memoised: a list re-render only re-renders rows
 * whose message changed. */
export const MessageRow = memo(function MessageRow(props: RowProps) {
  const { message } = props;
  return (
    <div
      className={`row row-${message.role}${props.isNew ? " row-new" : ""}`}
      data-id={message.id}
      data-role={message.role}
      data-status={message.status}
    >
      {props.divider && (
        <div className="divider" role="separator">
          {props.divider}
        </div>
      )}
      {message.role === "user" ? <UserBubble {...props} /> : <AssistantRow {...props} />}
    </div>
  );
});

/** One photo, or the grey square it will be. `tile` fills a grid cell. */
function BubbleImage({
  store,
  imageKey,
  onImageLoad,
  tile = false,
}: {
  store: ChatStore;
  imageKey: string | undefined;
  onImageLoad: () => void;
  tile?: boolean;
}) {
  const url = useImageUrl(store, imageKey);
  const cls = tile ? "bubble-tile" : "bubble-image";
  return url ? (
    <img className={cls} src={url} alt="Attached photo" onLoad={onImageLoad} />
  ) : (
    <div className={`${cls} placeholder-image`} aria-label="Photo loading" />
  );
}

function UserBubble({ message, store, failedReply, onImageLoad }: RowProps) {
  // A server row's slots follow its attachment ids (so a bubble shows a
  // placeholder per photo still arriving); this client's own send has keys only.
  const ids = attachmentIdsOf(message);
  const keys = ids.length > 0 ? ids.map((id) => `att:${id}`) : imageKeysOf(message);
  const have = imageKeysOf(message);
  return (
    <>
      <div className="user-line">
        {failedReply && (
          <button
            type="button"
            className="retry-arrow"
            aria-label="Retry"
            title="Retry"
            onClick={() => store.retry(failedReply.id)}
          >
            ↻
          </button>
        )}
        <div className="bubble bubble-user">
          {keys.length === 1 && (
            <BubbleImage store={store} imageKey={have.includes(keys[0]) ? keys[0] : undefined} onImageLoad={onImageLoad} />
          )}
          {keys.length > 1 && (
            <div className={`bubble-grid bubble-grid-${keys.length}`} data-testid="bubble-grid">
              {keys.map((key) => (
                <BubbleImage
                  key={key}
                  store={store}
                  imageKey={have.includes(key) ? key : undefined}
                  onImageLoad={onImageLoad}
                  tile
                />
              ))}
            </div>
          )}
          {message.quote && (
            <blockquote className="user-quote" data-testid="user-quote">
              <span className="user-quote-emoji" aria-hidden="true">
                {message.quote.emoji}
              </span>
              <span className="user-quote-title">{message.quote.title}</span>
            </blockquote>
          )}
          {message.text && <div className="user-text">{message.text}</div>}
        </div>
      </div>
      {failedReply && <ErrorCaption text={failedReply.text} className="failed-caption" />}
    </>
  );
}

function AssistantRow({ message, store, pairedAway }: RowProps) {
  switch (message.status) {
    case "done":
      // STR-8: a tools-only turn gets no bubble, not an empty pill.
      if (!message.text.trim()) return null;
      return (
        <div className="assistant-line">
          <Copyable text={message.text} className="bubble bubble-assistant">
            <SettledMarkdown text={message.text} />
          </Copyable>
        </div>
      );
    case "error":
      if (pairedAway) return null;
      return (
        <div className="assistant-error">
          <ErrorCaption text={message.text} className="inline-error" />
          {message.clientMsgId && (
            <button type="button" className="link-button" onClick={() => store.retry(message.id)}>
              Retry
            </button>
          )}
        </div>
      );
    case "stalled":
      return <div className="status-line">{message.text}</div>;
    default:
      return <InFlight message={message} store={store} />;
  }
}

/** The placeholder while a turn is in flight. The only part of the list that
 * re-renders per typewriter tick (STR-12): it subscribes to its own stream,
 * and its markdown is parsed uncached. */
function InFlight({ message, store }: { message: ChatMessage; store: ChatStore }) {
  const stream = useStream(store, message.id);
  const text = stream?.text ?? "";
  const pieces = text ? segments(text, stream?.marks ?? []) : [];
  const label = message.status === "sending" ? "Sending…" : (stream?.label ?? (text ? WRITING : THINKING));
  // STR-11: once text streams, the generic labels hide; a tool label stays,
  // because it is then the only thing explaining why the text stopped.
  // (The condition was inverted: it showed "Thinking…"/"Writing…" over streamed text
  // and hid a tool label there. Found by the STR-11 e2e test.)
  const showIndicator = !text || !describesUnseenWork(stream?.label);
  return (
    <>
      {pieces.map((piece, i) => (
        // STR-4: each piece between tool calls is its own block.
        <div className="assistant-line" key={i}>
          <div className="bubble bubble-assistant">
            <Markdown text={piece} />
          </div>
        </div>
      ))}
      {showIndicator && (
        <div className="working" aria-live="polite">
          <span className="working-dots" aria-hidden="true">
            <i />
            <i />
            <i />
          </span>
          <span className="working-label">{label}</span>
        </div>
      )}
    </>
  );
}

function ErrorCaption({ text, className }: { text: string; className: string }) {
  // RND-4: the error text is copyable (press and hold); it is what gets pasted
  // into a debugging session.
  return (
    <Copyable text={text} className={className}>
      <span className="error-text">{text}</span>
    </Copyable>
  );
}

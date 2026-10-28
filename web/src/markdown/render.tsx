// Pure token -> React mapping for markdown. SECURITY (portable-web-client.md,
// RND-5): markdown never becomes HTML. We only ever build React elements from
// the lexer's token tree; raw HTML tokens render as their literal text, images
// are never loaded, and only http/https/mailto links become anchors.
import { Fragment, type ReactNode } from "react";
import { Lexer, type Token, type Tokens } from "marked";

const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

/** Returns a normalised safe href, or null. Strict: the raw string must begin
 *  with an allowed scheme (no leading whitespace, no relative or
 *  protocol-less forms), and the parsed protocol must be allowed. */
export function safeHref(href: string): string | null {
  if (!/^(https?|mailto):/i.test(href)) return null;
  try {
    const url = new URL(href);
    return SAFE_PROTOCOLS.has(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

export function parseMarkdown(text: string): Token[] {
  return Lexer.lex(text, { gfm: true, breaks: false });
}

function renderInline(tokens: Token[] | undefined): ReactNode {
  if (!tokens) return null;
  return tokens.map((t, i) => <Fragment key={i}>{inlineToken(t)}</Fragment>);
}

function inlineToken(t: Token): ReactNode {
  switch (t.type) {
    case "text":
    case "escape": {
      const tt = t as Tokens.Text;
      return tt.tokens ? renderInline(tt.tokens) : tt.text;
    }
    case "strong":
      return <strong>{renderInline((t as Tokens.Strong).tokens)}</strong>;
    case "em":
      return <em>{renderInline((t as Tokens.Em).tokens)}</em>;
    case "del":
      return <del>{renderInline((t as Tokens.Del).tokens)}</del>;
    case "codespan":
      return <code className="md-code">{(t as Tokens.Codespan).text}</code>;
    case "br":
      return <br />;
    case "link": {
      const l = t as Tokens.Link;
      const href = safeHref(l.href);
      if (href === null) return <span>{renderInline(l.tokens)}</span>;
      return (
        <a href={href} rel="noopener noreferrer" target="_blank">
          {renderInline(l.tokens)}
        </a>
      );
    }
    case "image": {
      // Never loaded: could be a tracking pixel. Show the alt text.
      const im = t as Tokens.Image;
      return <span className="md-image-alt">{im.text}</span>;
    }
    case "html":
      // Raw HTML (inline or block) is literal text, never markup.
      return t.raw;
    default:
      return t.raw;
  }
}

function listItem(item: Tokens.ListItem, i: number): ReactNode {
  return <li key={i}>{renderBlocks(item.tokens, true)}</li>;
}

function blockToken(t: Token, tight: boolean): ReactNode {
  switch (t.type) {
    case "space":
    case "def":
      return null;
    case "paragraph": {
      const p = t as Tokens.Paragraph;
      return <p>{renderInline(p.tokens)}</p>;
    }
    case "text": {
      // Tight list item content, or stray text block.
      const tt = t as Tokens.Text;
      const inner = tt.tokens ? renderInline(tt.tokens) : tt.text;
      return tight ? <>{inner}</> : <p>{inner}</p>;
    }
    case "heading": {
      const h = t as Tokens.Heading;
      const level = Math.min(6, Math.max(1, h.depth));
      const Tag = `h${level}` as "h1";
      return <Tag>{renderInline(h.tokens)}</Tag>;
    }
    case "code": {
      const c = t as Tokens.Code;
      return (
        <pre className="md-pre">
          <code className="md-code-block">{c.text}</code>
        </pre>
      );
    }
    case "blockquote":
      return <blockquote>{renderBlocks((t as Tokens.Blockquote).tokens, false)}</blockquote>;
    case "hr":
      return <hr />;
    case "html":
      return <p className="md-raw">{t.raw}</p>;
    case "list": {
      const l = t as Tokens.List;
      const items = l.items.map(listItem);
      if (l.ordered) {
        const start = typeof l.start === "number" ? l.start : 1;
        return <ol start={start}>{items}</ol>;
      }
      return <ul>{items}</ul>;
    }
    case "table": {
      const tb = t as Tokens.Table;
      const align = (a: string | null) => (a === "left" || a === "right" || a === "center" ? a : undefined);
      const cls = (a: string | null) => {
        const v = align(a);
        return v ? `md-align-${v}` : undefined;
      };
      return (
        <div className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {tb.header.map((c, i) => (
                  <th key={i} className={cls(tb.align[i] ?? null)}>
                    {renderInline(c.tokens)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {tb.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((c, i) => (
                    <td key={i} className={cls(tb.align[i] ?? null)}>
                      {renderInline(c.tokens)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
    }
    default:
      // Unknown token types render their raw source as text.
      return <p>{t.raw}</p>;
  }
}

function renderBlocks(tokens: Token[], tight: boolean): ReactNode {
  return tokens.map((t, i) => <Fragment key={i}>{blockToken(t, tight)}</Fragment>);
}

export function renderTokens(tokens: Token[]): ReactNode {
  return renderBlocks(tokens, false);
}

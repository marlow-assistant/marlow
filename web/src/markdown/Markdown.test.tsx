import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { Markdown, SettledMarkdown } from "./Markdown";

// Mirrors scripts/dev/fake_claude.py MARKDOWN (the hostile sample).
const SAMPLE = `## A heading

Some **bold**, some *italic*, and \`inline code\`. A [safe link](https://example.com)
and a [javascript link](javascript:alert(1)) that must not be clickable.

- one
- two
  - nested

1. first
2. second

\`\`\`python
def hello():
    return "world"
\`\`\`

| col | value |
|-----|-------|
| a   | 1     |

<img src=x onerror="alert('raw html must render as text')">
`;

function r(text: string) {
  return render(<Markdown text={text} />).container;
}

describe("constructs", () => {
  it("paragraphs, headings 1-6, inline styles", () => {
    const c = r("# h1\n\n###### h6\n\npara **b** *i* ~~s~~ `c`");
    expect(c.querySelector("h1")?.textContent).toBe("h1");
    expect(c.querySelector("h6")?.textContent).toBe("h6");
    expect(c.querySelector("p strong")?.textContent).toBe("b");
    expect(c.querySelector("p em")?.textContent).toBe("i");
    expect(c.querySelector("p del")?.textContent).toBe("s");
    expect(c.querySelector("p code")?.textContent).toBe("c");
  });

  it("fenced and indented code in pre>code, no wrap class", () => {
    const c = r("```js\nconst a = 1;\n```\n\n    indented\n");
    const pres = c.querySelectorAll("pre > code");
    expect(pres.length).toBe(2);
    expect(pres[0].textContent).toBe("const a = 1;");
    expect(pres[1].textContent?.trim()).toBe("indented");
  });

  it("lists, nesting, ol start", () => {
    const c = r("- a\n- b\n  - nested\n\ntext\n\n4. four\n5. five");
    expect(c.querySelectorAll("ul").length).toBe(2);
    expect(c.querySelector("ul ul li")?.textContent).toBe("nested");
    const ol = c.querySelector("ol");
    expect(ol?.getAttribute("start")).toBe("4");
    expect(ol?.querySelectorAll("li").length).toBe(2);
  });

  it("long list item is not truncated or clamped", () => {
    const long = "word ".repeat(300).trim();
    const c = r(`- ${long}`);
    const li = c.querySelector("li")!;
    expect(li.textContent).toBe(long);
    expect(c.innerHTML).not.toMatch(/ellipsis|clamp|truncate/i);
  });

  it("blockquote, hr, table, line break", () => {
    const c = r("> quoted\n\n---\n\n| a | b |\n|---|--:|\n| 1 | 2 |\n\nx  \ny");
    expect(c.querySelector("blockquote")?.textContent).toContain("quoted");
    expect(c.querySelector("hr")).not.toBeNull();
    expect(c.querySelectorAll("th").length).toBe(2);
    expect(c.querySelector("td")?.textContent).toBe("1");
    expect(c.querySelectorAll("tbody td")[1].className).toContain("md-align-right");
    expect(c.querySelector("br")).not.toBeNull();
  });

  it("unclosed fence while streaming renders the rest as code", () => {
    const c = r("intro\n\n```py\nprint(1)\nprint(2)");
    expect(c.querySelector("pre code")?.textContent).toBe("print(1)\nprint(2)");
  });

  it("no style attributes anywhere", () => {
    expect(r(SAMPLE).querySelector("[style]")).toBeNull();
  });

  it("SettledMarkdown renders the same output", () => {
    const a = render(<SettledMarkdown text="**x**" />).container.innerHTML;
    expect(a).toBe(r("**x**").innerHTML);
  });
});

describe("hostile sample", () => {
  it("renders safely", () => {
    const c = r(SAMPLE);
    expect(c.querySelectorAll("img").length).toBe(0);
    expect(c.querySelector("[onerror]")).toBeNull();
    expect(c.textContent).toContain("<img src=x onerror=");
    const anchors = [...c.querySelectorAll("a")];
    expect(anchors.length).toBe(1);
    expect(anchors[0].getAttribute("href")).toBe("https://example.com/");
    expect(anchors[0].getAttribute("rel")).toBe("noopener noreferrer");
    expect(anchors[0].getAttribute("target")).toBe("_blank");
    expect(c.textContent).toContain("javascript link");
    expect(c.innerHTML).not.toContain("javascript:");
  });
});

describe("link hrefs", () => {
  const bad: [string, string][] = [
    ["javascript", "javascript:alert(1)"],
    ["data", "data:text/html,<b>x</b>"],
    ["vbscript", "vbscript:msgbox(1)"],
    ["mixed case", "JaVaScRiPt:alert(1)"],
    ["leading space", "<%20javascript:alert(1)>"],
    ["leading space angle", "< javascript:alert(1)>"],
    ["tab inside", "java\tscript:alert(1)"],
    ["encoded tab", "java&#9;script:alert(1)"],
    ["entity", "&#106;avascript:alert(1)"],
    ["entity colon", "javascript&colon;alert(1)"],
    ["protocol-relative", "//evil.com"],
    ["relative", "/v1/permission/x"],
    ["bare", "example.com"],
  ];
  it.each(bad)("%s is not an anchor", (_n, href) => {
    const c = r(`[click](${href})`);
    expect(c.querySelector("a")).toBeNull();
    expect(c.textContent).toContain("click");
    expect(c.innerHTML.toLowerCase()).not.toMatch(/href=["']?\s*(javascript|data|vbscript)/);
  });

  it.each([
    ["http", "http://example.com/a"],
    ["https", "https://example.com/a"],
    ["mailto", "mailto:a@example.com"],
    ["upper-case scheme", "HTTPS://example.com/a"],
  ])("%s is an anchor", (_n, href) => {
    const a = r(`[click](${href})`).querySelector("a");
    expect(a).not.toBeNull();
    expect(a!.getAttribute("rel")).toBe("noopener noreferrer");
    expect(a!.getAttribute("target")).toBe("_blank");
  });

  it("bare URL autolink follows the same rule", () => {
    expect(r("see https://example.com/x now").querySelector("a")?.getAttribute("href")).toBe(
      "https://example.com/x",
    );
    expect(r("see <javascript:alert(1)> now").querySelector("a")).toBeNull();
  });
});

describe("images and raw html", () => {
  it("markdown image renders alt text, no img", () => {
    const c = r("![tracking alt](https://tracker/pixel.png)");
    expect(c.querySelector("img")).toBeNull();
    expect(c.textContent).toContain("tracking alt");
  });

  it("block and inline html are literal text", () => {
    const c = r('<script>alert(1)</script>\n\ntext <b onclick="x()">bold</b> <iframe src=x></iframe>');
    expect(c.querySelector("script, b, iframe")).toBeNull();
    expect(c.textContent).toContain("<script>alert(1)</script>");
    expect(c.textContent).toContain('<b onclick="x()">');
  });
});

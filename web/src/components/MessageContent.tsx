import { Check, Copy } from "lucide-react";
import { useState } from "react";

export function MessageContent({ content }: { content: string }) {
  const sections: Array<{ kind: "text" | "code"; value: string; language?: string }> = [];
  const lines = content.split("\n");
  let text: string[] = [];
  let code: string[] | null = null;
  let language = "";
  for (const line of lines) {
    if (code === null && line.startsWith("```")) {
      if (text.length) sections.push({ kind: "text", value: text.join("\n") });
      text = [];
      code = [];
      language = line.slice(3).trim().slice(0, 24);
    } else if (code !== null && line.startsWith("```")) {
      sections.push({ kind: "code", value: code.join("\n"), language });
      code = null;
      language = "";
    } else if (code !== null) {
      code.push(line);
    } else {
      text.push(line);
    }
  }
  if (code !== null) {
    sections.push({ kind: "code", value: code.join("\n"), language });
  }
  if (text.length) sections.push({ kind: "text", value: text.join("\n") });

  return (
    <div className="message-content">
      {sections.map((section, index) => section.kind === "code" ? (
        <CodeBlock key={`${index}-${section.language}`} code={section.value} language={section.language} />
      ) : (
        <p className="message-prose" key={index}>{section.value}</p>
      ))}
    </div>
  );
}

function CodeBlock({ code, language }: { code: string; language?: string }) {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setError(false);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setError(true);
    }
  };
  return (
    <div className="code-block">
      <div className="code-toolbar">
        <span>{language || "Code"}</span>
        <button type="button" className="copy-button" onClick={() => void copy()} aria-label="Copy code">
          {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre
        tabIndex={0}
        aria-label={language ? `${language} code block` : "Code block"}
      ><code>{code}</code></pre>
      {error && <span role="status" className="copy-error">Clipboard access is unavailable.</span>}
    </div>
  );
}

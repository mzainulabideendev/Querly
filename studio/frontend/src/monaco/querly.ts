import { Monaco } from "@monaco-editor/react";

export function registerQuerlyLanguage(monaco: Monaco) {
  // Register English syntax
  monaco.languages.register({ id: "querly-english" });

  monaco.languages.setMonarchTokensProvider("querly-english", {
    ignoreCase: true,
    keywords: [
      "CREATE", "TABLE", "GET", "ALL", "FROM", "WHERE", "ORDER", "BY",
      "LIMIT", "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE",
      "AS", "WITH", "REQUIRED", "OPTIONAL", "PRIMARY", "KEY", "AUTO",
      "UNIQUE", "SORT", "ASCENDING", "DESCENDING", "AND", "OR", "NOT",
      "IS", "GREATER", "THAN", "LESS", "EQUALS", "LIKE", "RETURNING"
    ],
    operators: ["=", "!=", ">", "<", ">=", "<=", "+", "-", "*", "/", "%"],
    symbols: /[=><!~?:&|+\-*\/\^%]+/,
    tokenizer: {
      root: [
        [/[a-z_$][\w$]*/, {
          cases: {
            "@keywords": "keyword",
            "@default": "identifier",
          },
        }],
        { include: "@whitespace" },
        [/[{}()\[\]]/, "@brackets"],
        [/@symbols/, {
          cases: {
            "@operators": "operator",
            "@default": "",
          },
        }],
        [/\d*\.\d+([eE][\-+]?\d+)?/, "number.float"],
        [/\d+/, "number"],
        [/[;,.]/, "delimiter"],
        [/"([^"\\]|\\.)*$/, "string.invalid"],
        [/"/, { token: "string.quote", bracket: "@open", next: "@string" }],
        [/'/, { token: "string.quote", bracket: "@open", next: "@string_sq" }],
      ],
      string: [
        [/[^\\"]+/, "string"],
        [/\\./, "string.escape"],
        [/"/, { token: "string.quote", bracket: "@close", next: "@pop" }],
      ],
      string_sq: [
        [/[^\\']+/, "string"],
        [/\\./, "string.escape"],
        [/'/, { token: "string.quote", bracket: "@close", next: "@pop" }],
      ],
      whitespace: [
        [/[ \t\r\n]+/, "white"],
        [/--.*$/, "comment"],
        [/\/\*/, "comment", "@comment"],
      ],
      comment: [
        [/[^\/*]+/, "comment"],
        [/\/\*/, "comment", "@push"],
        ["\\*/", "comment", "@pop"],
        [/[\/*]/, "comment"],
      ],
    },
  });

  // Register Structured syntax
  monaco.languages.register({ id: "querly-structured" });

  monaco.languages.setMonarchTokensProvider("querly-structured", {
    keywords: [
      "table", "query", "from", "select", "where", "order", "by", "limit",
      "insert", "update", "delete", "into", "values", "set", "returning",
      "integer", "text", "timestamp", "bool", "email", "url", "json",
      "primary", "auto", "required", "unique", "default", "now"
    ],
    operators: ["=", "!=", ">", "<", ">=", "<=", "+", "-", "*", "/", "%", ":"],
    symbols: /[=><!~?:&|+\-*\/\^%]+/,
    tokenizer: {
      root: [
        [/[a-z_$][\w$]*/, {
          cases: {
            "@keywords": "keyword",
            "@default": "identifier",
          },
        }],
        [/\$[a-zA-Z0-9_]+/, "variable"],
        { include: "@whitespace" },
        [/[{}()\[\]]/, "@brackets"],
        [/@symbols/, {
          cases: {
            "@operators": "operator",
            "@default": "",
          },
        }],
        [/\d*\.\d+([eE][\-+]?\d+)?/, "number.float"],
        [/\d+/, "number"],
        [/[;,.]/, "delimiter"],
        [/"([^"\\]|\\.)*$/, "string.invalid"],
        [/"/, { token: "string.quote", bracket: "@open", next: "@string" }],
      ],
      string: [
        [/[^\\"]+/, "string"],
        [/\\./, "string.escape"],
        [/"/, { token: "string.quote", bracket: "@close", next: "@pop" }],
      ],
      whitespace: [
        [/[ \t\r\n]+/, "white"],
        [/--.*$/, "comment"],
        [/\/\*/, "comment", "@comment"],
      ],
      comment: [
        [/[^\/*]+/, "comment"],
        [/\/\*/, "comment", "@push"],
        ["\\*/", "comment", "@pop"],
        [/[\/*]/, "comment"],
      ],
    },
  });

  // Common Theme
  monaco.editor.defineTheme("querly-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "keyword", foreground: "bb86fc", fontStyle: "bold" },
      { token: "identifier", foreground: "e0e0e0" },
      { token: "string", foreground: "ce9178" },
      { token: "number", foreground: "b5cea8" },
      { token: "operator", foreground: "569cd6" },
      { token: "comment", foreground: "6a9955", fontStyle: "italic" },
      { token: "variable", foreground: "9cdcfe" },
    ],
    colors: {
      "editor.background": "#1e1e1e",
      "editor.foreground": "#d4d4d4",
      "editorLineNumber.foreground": "#858585",
      "editorCursor.foreground": "#bb86fc",
      "editor.selectionBackground": "#264f78",
    },
  });
}

import type { Monaco } from "@monaco-editor/react";

const LANGUAGE_ID = "querly";

const KEYWORDS = [
  // declarations
  "table", "query", "view", "index",
  // English source syntax
  "CREATE", "TABLE", "WITH", "GET", "ALL", "FROM", "WHERE", "ORDER", "SORT",
  "BY", "LIMIT", "OFFSET", "INSERT", "INTO", "VALUES", "UPDATE", "SET",
  "DELETE", "RETURNING", "AS", "REQUIRED", "OPTIONAL", "PRIMARY", "KEY",
  "AUTO", "UNIQUE", "ASCENDING", "DESCENDING", "AND", "OR", "NOT", "IS",
  "GREATER", "THAN", "LESS", "EQUALS", "LIKE", "BETWEEN", "IN", "EXISTS",
  "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "ON", "GROUP", "HAVING",
  "DISTINCT", "UNION", "CASE", "WHEN", "THEN", "ELSE", "END", "NULL",
  "TRUE", "FALSE", "ASC", "DESC",
  // structured source syntax
  "select", "insert", "update", "delete", "into", "values", "set", "from",
  "where", "order", "group", "limit", "offset", "returning", "join", "on",
  "and", "or", "not", "is", "between", "in", "like", "as", "asc", "desc",
  "required", "optional", "primary", "key", "auto", "unique", "default",
  "references", "now", "null", "true", "false", "distinct", "case", "when",
  "then", "else", "end",
];

const TYPE_KEYWORDS = [
  "integer", "int", "number", "float", "double", "decimal", "numeric",
  "text", "varchar", "string", "char", "boolean", "bool", "date", "time",
  "timestamp", "datetime", "duration", "interval", "uuid", "email", "url",
  "json", "jsonb", "binary", "blob", "bytes", "geo", "geometry", "vector",
  // uppercase variants used by the English syntax
  "INTEGER", "INT", "NUMBER", "FLOAT", "DOUBLE", "DECIMAL", "NUMERIC",
  "TEXT", "VARCHAR", "STRING", "CHAR", "BOOLEAN", "BOOL", "DATE", "TIME",
  "TIMESTAMP", "DATETIME", "DURATION", "UUID", "EMAIL", "URL", "JSON",
  "BINARY", "BLOB", "BYTES", "GEO", "VECTOR",
];

const FUNCTIONS = [
  "now", "current_timestamp", "count", "sum", "avg", "min", "max",
  "coalesce", "lower", "upper", "length", "trim", "abs", "round",
  "NOW", "CURRENT_TIMESTAMP", "COUNT", "SUM", "AVG", "MIN", "MAX",
  "COALESCE", "LOWER", "UPPER", "LENGTH", "TRIM", "ABS", "ROUND",
];

const SNIPPETS = [
  {
    label: "table-schema",
    detail: "Querly table declaration (structured)",
    insertText:
      "table ${1:users} {\n  ${2:id}: integer @primary @auto\n  ${3:name}: text @required\n  ${4:email}: email @unique\n}\n",
  },
  {
    label: "query-select",
    detail: "Querly query (structured)",
    insertText:
      "query ${1:adults} {\n  from ${2:users}\n  select ${3:id, name}\n  where ${4:age > 18}\n  order by ${5:name} asc\n  limit ${6:50}\n}\n",
  },
  {
    label: "insert-row",
    detail: "Insert statement (structured)",
    insertText:
      "insert into ${1:users} (${2:name, email})\nvalues (${3:\"Ada\", \"ada@example.com\"})\nreturning ${4:id}\n",
  },
  {
    label: "create-table-english",
    detail: "Querly table declaration (English)",
    insertText:
      "CREATE TABLE ${1:users} WITH:\n  ${2:id} AS REQUIRED NUMBER PRIMARY KEY AUTO\n  ${3:name} AS REQUIRED TEXT\n  ${4:email} AS REQUIRED TEXT UNIQUE\n",
  },
  {
    label: "get-english",
    detail: "Querly SELECT (English)",
    insertText:
      "GET ${1:id, name} FROM ${2:users}\nWHERE ${3:age} IS GREATER THAN ${4:18}\nSORT BY ${5:name} ASCENDING\nLIMIT ${6:50};\n",
  },
];

let registered = false;

export function registerQuerlyLanguage(monaco: Monaco) {
  if (registered) {
    return;
  }
  registered = true;

  monaco.languages.register({
    id: LANGUAGE_ID,
    extensions: [".querly", ".oq", ".qly"],
    aliases: ["Querly", "querly"],
  });

  monaco.languages.setLanguageConfiguration(LANGUAGE_ID, {
    comments: { lineComment: "--", blockComment: ["/*", "*/"] },
    brackets: [
      ["{", "}"],
      ["[", "]"],
      ["(", ")"],
    ],
    autoClosingPairs: [
      { open: "{", close: "}" },
      { open: "[", close: "]" },
      { open: "(", close: ")" },
      { open: '"', close: '"' },
      { open: "'", close: "'" },
      { open: "`", close: "`" },
    ],
    surroundingPairs: [
      { open: "{", close: "}" },
      { open: "[", close: "]" },
      { open: "(", close: ")" },
      { open: '"', close: '"' },
      { open: "'", close: "'" },
    ],
    indentationRules: {
      increaseIndentPattern: /(\{|\bWITH\s*:)\s*$/i,
      decreaseIndentPattern: /^\s*\}/,
    },
  });

  monaco.languages.setMonarchTokensProvider(LANGUAGE_ID, {
    ignoreCase: true,
    defaultToken: "",
    keywords: KEYWORDS,
    typeKeywords: TYPE_KEYWORDS,
    functions: FUNCTIONS,
    operators: ["=", "!=", "<>", ">", "<", ">=", "<=", "+", "-", "*", "/", "%", ":", "@"],
    symbols: /[=><!~?:&|+\-*/^%@]+/,
    tokenizer: {
      root: [
        [/--.*$/, "comment"],
        [/\/\*/, "comment", "@comment"],
        [/[a-zA-Z_$][\w$]*/, {
          cases: {
            "@keywords": "keyword",
            "@typeKeywords": "type",
            "@functions": "predefined",
            "@default": "identifier",
          },
        }],
        [/\$[a-zA-Z0-9_]+/, "variable"],
        [/@[a-zA-Z_][\w]*/, "annotation"],
        { include: "@whitespace" },
        [/[{}()[\]]/, "@brackets"],
        [/@symbols/, {
          cases: {
            "@operators": "operator",
            "@default": "",
          },
        }],
        [/\d*\.\d+([eE][-+]?\d+)?/, "number.float"],
        [/\d+/, "number"],
        [/[;,.]/, "delimiter"],
        [/"([^"\\]|\\.)*$/, "string.invalid"],
        [/"/, { token: "string.quote", bracket: "@open", next: "@string" }],
        [/'([^'\\]|\\.)*$/, "string.invalid"],
        [/'/, { token: "string.quote", bracket: "@open", next: "@string_sq" }],
      ],
      whitespace: [
        [/[ \t\r\n]+/, "white"],
      ],
      comment: [
        [/[^/*]+/, "comment"],
        [/\/\*/, "comment", "@push"],
        [/\*\//, "comment", "@pop"],
        [/[/*]/, "comment"],
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
    },
  });

  monaco.languages.registerCompletionItemProvider(LANGUAGE_ID, {
    triggerCharacters: ["@", "$", " "],
    provideCompletionItems(model: any, position: any) {
      const word = model.getWordUntilPosition(position);
      const range = {
        startLineNumber: position.lineNumber,
        endLineNumber: position.lineNumber,
        startColumn: word.startColumn,
        endColumn: word.endColumn,
      };

      const keywordSuggestions = [...KEYWORDS, ...TYPE_KEYWORDS, ...FUNCTIONS].map(
        (keyword) => ({
          label: keyword,
          kind: monaco.languages.CompletionItemKind.Keyword,
          insertText: keyword,
          range,
        }),
      );

      const snippetSuggestions = SNIPPETS.map((snippet) => ({
        label: snippet.label,
        detail: snippet.detail,
        kind: monaco.languages.CompletionItemKind.Snippet,
        insertText: snippet.insertText,
        insertTextRules:
          monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet,
        range,
      }));

      return { suggestions: [...snippetSuggestions, ...keywordSuggestions] };
    },
  });

  monaco.editor.defineTheme("querly-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "keyword", foreground: "c792ea", fontStyle: "bold" },
      { token: "type", foreground: "82aaff" },
      { token: "predefined", foreground: "ffcb6b" },
      { token: "identifier", foreground: "e6e6e6" },
      { token: "annotation", foreground: "f78c6c" },
      { token: "string", foreground: "c3e88d" },
      { token: "number", foreground: "f78c6c" },
      { token: "operator", foreground: "89ddff" },
      { token: "delimiter", foreground: "89ddff" },
      { token: "comment", foreground: "5f7e97", fontStyle: "italic" },
      { token: "variable", foreground: "ffcb6b" },
    ],
    colors: {
      "editor.background": "#14161c",
      "editor.foreground": "#d7dbe0",
      "editorLineNumber.foreground": "#4b5563",
      "editorLineNumber.activeForeground": "#c792ea",
      "editorCursor.foreground": "#c792ea",
      "editor.selectionBackground": "#2d3a52",
      "editor.lineHighlightBackground": "#1c2029",
      "editorIndentGuide.background1": "#242a36",
      "editorBracketMatch.background": "#2d3a52",
    },
  });
}

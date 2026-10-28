'use client';

import CodeMirror from '@uiw/react-codemirror';
import { javascript } from '@codemirror/lang-javascript';
import { python } from '@codemirror/lang-python';
import { json } from '@codemirror/lang-json';
import { css } from '@codemirror/lang-css';
import { html } from '@codemirror/lang-html';
import { markdown } from '@codemirror/lang-markdown';
import { yaml } from '@codemirror/lang-yaml';
import { oneDark } from '@codemirror/theme-one-dark';
import { useMemo } from 'react';

function languageForPath(path: string) {
  const extension = path.split('.').pop()?.toLowerCase();
  switch (extension) {
    case 'py':
      return python();
    case 'js':
    case 'jsx':
    case 'ts':
    case 'tsx':
    case 'mjs':
    case 'cjs':
      return javascript({ jsx: ['jsx', 'tsx'].includes(extension), typescript: ['ts', 'tsx'].includes(extension) });
    case 'json':
      return json();
    case 'css':
    case 'scss':
    case 'less':
      return css();
    case 'html':
    case 'htm':
    case 'xml':
      return html();
    case 'md':
    case 'mdx':
      return markdown();
    case 'yaml':
    case 'yml':
      return yaml();
    default:
      return [];
  }
}

export default function ReviewCodeEditor({
  path,
  value,
  onChange,
  onSelectionChange,
  disabled,
}: {
  path: string;
  value: string;
  onChange: (value: string) => void;
  onSelectionChange: (line: number, selectedText: string) => void;
  disabled: boolean;
}) {
  const extensions = useMemo(() => [languageForPath(path)], [path]);

  return (
    <div className="review-code-editor">
      <CodeMirror
        value={value}
        height="min(62vh, 680px)"
        theme={oneDark}
        extensions={extensions}
        onChange={onChange}
        onUpdate={(update) => {
          if (update.selectionSet || update.docChanged) {
            const selection = update.state.selection.main;
            onSelectionChange(
              update.state.doc.lineAt(selection.head).number,
              update.state.sliceDoc(selection.from, selection.to),
            );
          }
        }}
        basicSetup={{
          lineNumbers: true,
          highlightActiveLine: true,
          highlightActiveLineGutter: true,
          foldGutter: true,
          bracketMatching: true,
          closeBrackets: true,
          autocompletion: true,
          indentOnInput: true,
        }}
        editable={!disabled}
        readOnly={disabled}
        aria-label={`Code editor for ${path}`}
      />
      <style jsx global>{`
        .review-code-editor .cm-editor {
          border: 1px solid #313a49;
          border-radius: 0 0 8px 8px;
          font-size: 13px;
        }
        .review-code-editor .cm-editor.cm-focused {
          outline: 1px solid #6d79d8;
        }
        .review-code-editor .cm-scroller {
          font-family: var(--font-mono), ui-monospace, SFMono-Regular, Consolas, monospace;
        }
      `}</style>
    </div>
  );
}

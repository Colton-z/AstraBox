import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { BundledLanguage } from "shiki";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowLeftIcon, DownloadIcon, RefreshCcwIcon } from "lucide-react";
import {
  buildSessionFileDownloadUrl,
  previewSessionFile,
  readSessionFile,
} from "@/api";
import {
  CodeBlock,
  CodeBlockCopyButton,
} from "@/components/ai-elements/code-block";
import {
  WebPreview,
  WebPreviewBody,
} from "@/components/ai-elements/web-preview";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ErrorNote } from "@/components/shell";

const imageTypes: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  svg: "image/svg+xml",
  avif: "image/avif",
  ico: "image/x-icon",
};
const languages: Record<string, BundledLanguage> = {
  js: "javascript",
  jsx: "jsx",
  mjs: "javascript",
  cjs: "javascript",
  ts: "typescript",
  tsx: "tsx",
  json: "json",
  py: "python",
  sh: "bash",
  bash: "bash",
  html: "html",
  htm: "html",
  css: "css",
  scss: "scss",
  md: "markdown",
  markdown: "markdown",
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  xml: "xml",
  sql: "sql",
  go: "go",
  rs: "rust",
  java: "java",
  c: "c",
  cpp: "cpp",
  h: "c",
  rb: "ruby",
  php: "php",
};

type Content = { text?: string; url?: string };

export function SessionFileViewer({
  sessionId,
  path,
  refreshSignal,
  onBack,
  enabled,
  disabledMessage,
}: {
  sessionId: string;
  path: string;
  refreshSignal?: number;
  onBack: () => void;
  enabled: boolean;
  disabledMessage: string;
}) {
  const { t } = useTranslation();
  const name = path.split("/").pop() || path;
  const extension = name.split(".").pop()?.toLowerCase() || "";
  const html = extension === "html" || extension === "htm";
  const markdown = extension === "md" || extension === "markdown";
  const imageType = imageTypes[extension];
  const pdf = extension === "pdf";
  const [mode, setMode] = useState("preview");
  const [revision, setRevision] = useState(0);
  const [content, setContent] = useState<Content | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | undefined;
    setContent(null);
    setError(null);
    if (!enabled) return () => controller.abort();
    void (async () => {
      try {
        if (html && mode === "preview") {
          const grant = await previewSessionFile(
            sessionId,
            path,
            controller.signal,
          );
          const response = await fetch(grant.url, {
            method: "HEAD",
            signal: controller.signal,
            credentials: "omit",
          });
          if (!response.ok)
            throw new Error(
              `${t("chat:files.preview_failed")} (HTTP ${response.status})`,
            );
          if (!controller.signal.aborted) setContent({ url: grant.url });
          return;
        }
        const blob = await readSessionFile(
          sessionId,
          path,
          (imageType || pdf ? 20 : 2) * 1024 * 1024,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        if (imageType || pdf) {
          objectUrl = URL.createObjectURL(
            new Blob([blob], { type: imageType || "application/pdf" }),
          );
          setContent({ url: objectUrl });
        } else {
          const bytes = await blob.arrayBuffer();
          let text: string;
          try {
            text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
          } catch {
            throw new Error(t("chat:files.preview_binary"));
          }
          if (text.includes("\0"))
            throw new Error(t("chat:files.preview_binary"));
          if (!controller.signal.aborted) setContent({ text });
        }
      } catch (cause) {
        if (!controller.signal.aborted)
          setError(cause instanceof Error ? cause.message : String(cause));
      }
    })();
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [
    sessionId,
    path,
    refreshSignal,
    revision,
    mode,
    enabled,
    html,
    imageType,
    pdf,
    t,
  ]);

  const body = !enabled ? (
    <ErrorNote>{disabledMessage}</ErrorNote>
  ) : error ? (
    <ErrorNote>{error}</ErrorNote>
  ) : !content ? (
    <div role="status" className="p-3 text-sm text-muted-foreground">
      {t("chat:files.preview_loading")}
    </div>
  ) : html && mode === "preview" ? (
    <WebPreview
      defaultUrl={content.url}
      className="h-full min-h-0 rounded-none border-0"
    >
      <WebPreviewBody
        src={content.url}
        title={name}
        sandbox="allow-scripts"
        referrerPolicy="no-referrer"
        className="h-full w-full bg-white"
      />
    </WebPreview>
  ) : imageType ? (
    <img
      src={content.url}
      alt={name}
      onError={() => setError(t("chat:files.preview_failed"))}
      className="max-h-full max-w-full object-contain"
    />
  ) : pdf ? (
    <iframe src={content.url} title={name} className="h-full w-full border-0" />
  ) : markdown && mode === "preview" ? (
    <div className="markdown p-4">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>
        {content.text ?? ""}
      </ReactMarkdown>
    </div>
  ) : (
    <CodeBlock
      code={content.text ?? ""}
      language={languages[extension] ?? ("text" as BundledLanguage)}
      showLineNumbers
      className="rounded-none border-0"
    >
      <CodeBlockCopyButton aria-label={t("common:copy")} />
    </CodeBlock>
  );

  return (
    <section
      aria-label={t("chat:files.file_content")}
      className="flex h-full min-h-0 flex-col"
    >
      <div className="flex shrink-0 items-center gap-2 border-b p-2">
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("chat:files.back_to_files")}
          onClick={onBack}
        >
          <ArrowLeftIcon className="size-4" />
        </Button>
        <span
          className="min-w-0 flex-1 truncate font-mono text-xs"
          title={path}
        >
          {name}
        </span>
        <Button
          variant="ghost"
          size="icon"
          aria-label={t("common:refresh")}
          disabled={!enabled}
          onClick={() => setRevision((value) => value + 1)}
        >
          <RefreshCcwIcon className="size-4" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          nativeButton={false}
          aria-label={t("chat:files.download")}
          disabled={!enabled}
          render={
            <a
              href={buildSessionFileDownloadUrl(sessionId, path)}
              download={name}
            />
          }
        >
          <DownloadIcon className="size-4" />
        </Button>
      </div>
      {html || markdown ? (
        <Tabs
          value={mode}
          onValueChange={setMode}
          className="flex min-h-0 flex-1 flex-col"
        >
          <TabsList variant="line" className="shrink-0 px-2">
            <TabsTrigger value="preview">{t("chat:files.preview")}</TabsTrigger>
            <TabsTrigger value="source">{t("chat:files.source")}</TabsTrigger>
          </TabsList>
          <TabsContent value="preview" className="min-h-0 flex-1 overflow-auto">
            {mode === "preview" && body}
          </TabsContent>
          <TabsContent value="source" className="min-h-0 flex-1 overflow-auto">
            {mode === "source" && body}
          </TabsContent>
        </Tabs>
      ) : (
        <div className="min-h-0 flex-1 overflow-auto">{body}</div>
      )}
    </section>
  );
}

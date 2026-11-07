import * as React from "react";
import {
  BotIcon,
  ExternalLinkIcon,
  LifeBuoyIcon,
  Loader2Icon,
  MessageSquareTextIcon,
  SendIcon,
  ThumbsDownIcon,
  ThumbsUpIcon,
  XIcon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import {
  type AssistantCitation,
  type AssistantMessage,
  ASSISTANT_OPEN_EVENT,
  type AssistantOpenEventDetail,
  type AssistantSurface,
  createAssistantConversation,
  createAssistantSupportRequest,
  loadAssistantMessages,
  sendAssistantFeedback,
  sendAssistantMessage,
} from "@/lib/assistant";
import { cn } from "@/lib/utils";

const CONVERSATION_STORAGE_KEY = "demo-platform_assistant_conversation_id";

type LocalMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: AssistantCitation[];
  pending?: boolean;
};

export function FloatingAssistantWidget({
  surface,
}: {
  surface: AssistantSurface;
}) {
  const [open, setOpen] = React.useState(() =>
    typeof window === "undefined"
      ? false
      : window.localStorage.getItem("demo-platform_assistant_open") === "1",
  );
  const [conversationId, setConversationId] = React.useState<string | null>(
    () =>
      typeof window === "undefined"
        ? null
        : window.localStorage.getItem(CONVERSATION_STORAGE_KEY),
  );
  const [messages, setMessages] = React.useState<LocalMessage[]>([]);
  const [input, setInput] = React.useState("");
  const [suggestedQuestions, setSuggestedQuestions] = React.useState<string[]>([
    "Как запустить нейрокомментинг?",
    "Сколько стоит тариф?",
    "Как безопасно импортировать аккаунты?",
  ]);
  const [loading, setLoading] = React.useState(false);
  const [booting, setBooting] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [supportOpen, setSupportOpen] = React.useState(false);
  const [supportContact, setSupportContact] = React.useState("");
  const [supportMessage, setSupportMessage] = React.useState("");
  const [supportBusy, setSupportBusy] = React.useState(false);
  const [supportNotice, setSupportNotice] = React.useState<string | null>(null);
  const listRef = React.useRef<HTMLDivElement | null>(null);

  React.useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem("demo-platform_assistant_open", open ? "1" : "0");
    }
  }, [open]);

  React.useEffect(() => {
    function handleOpen(event: Event) {
      const detail = (event as CustomEvent<AssistantOpenEventDetail>).detail;
      setOpen(true);
      if (detail?.support) {
        setSupportOpen(true);
        setSupportNotice(null);
        if (detail.message) {
          setSupportMessage((current) => current || detail.message || "");
        }
      }
    }

    window.addEventListener(ASSISTANT_OPEN_EVENT, handleOpen);
    return () => window.removeEventListener(ASSISTANT_OPEN_EVENT, handleOpen);
  }, []);

  React.useEffect(() => {
    if (!open || conversationId) return;
    let cancelled = false;
    setBooting(true);
    setError(null);
    createAssistantConversation(surface)
      .then((response) => {
        if (cancelled) return;
        setConversationId(response.conversation_id);
        window.localStorage.setItem(
          CONVERSATION_STORAGE_KEY,
          response.conversation_id,
        );
        setMessages([
          {
            id: "welcome",
            role: "assistant",
            content: response.welcome_message,
            citations: [],
          },
        ]);
      })
      .catch((caught) => {
        if (!cancelled) setError(errorMessage(caught));
      })
      .finally(() => {
        if (!cancelled) setBooting(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, open, surface]);

  React.useEffect(() => {
    if (!open || !conversationId || messages.length) return;
    let cancelled = false;
    setBooting(true);
    loadAssistantMessages(conversationId)
      .then((items) => {
        if (cancelled) return;
        setMessages(
          items.length
            ? items.map(fromApiMessage)
            : [
                {
                  id: "welcome",
                  role: "assistant",
                  content: "Привет! Я помогу разобраться с demo-platform.",
                  citations: [],
                },
              ],
        );
      })
      .catch(() => {
        window.localStorage.removeItem(CONVERSATION_STORAGE_KEY);
        setConversationId(null);
      })
      .finally(() => {
        if (!cancelled) setBooting(false);
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId, messages.length, open]);

  React.useEffect(() => {
    if (!open) return;
    listRef.current?.scrollTo({
      top: listRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, open]);

  async function submitMessage(text = input) {
    const value = text.trim();
    if (!value || loading || booting) return;
    setInput("");
    setError(null);
    let id = conversationId;
    if (!id) {
      try {
        id = (await createConversation()).conversation_id;
      } catch (caught) {
        setError(errorMessage(caught));
        setInput(value);
        return;
      }
    }
    const userMessage: LocalMessage = {
      id: `local_${crypto.randomUUID()}`,
      role: "user",
      content: value,
      citations: [],
    };
    const pendingMessage: LocalMessage = {
      id: `pending_${crypto.randomUUID()}`,
      role: "assistant",
      content: "Печатаю ответ...",
      citations: [],
      pending: true,
    };
    setMessages((current) => [...current, userMessage, pendingMessage]);
    setLoading(true);
    try {
      const response = await sendAssistantMessage(id, surface, value);
      setSuggestedQuestions(response.suggested_questions || []);
      setMessages((current) =>
        current.map((message) =>
          message.id === pendingMessage.id
            ? {
                id: response.message_id,
                role: "assistant",
                content: response.answer,
                citations: response.citations || [],
              }
            : message,
        ),
      );
    } catch (caught) {
      setError(errorMessage(caught));
      setMessages((current) =>
        current.filter((message) => message.id !== pendingMessage.id),
      );
    } finally {
      setLoading(false);
    }
  }

  async function createConversation() {
    const response = await createAssistantConversation(surface);
    setConversationId(response.conversation_id);
    window.localStorage.setItem(
      CONVERSATION_STORAGE_KEY,
      response.conversation_id,
    );
    return response;
  }

  async function submitSupportRequest(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const contact = supportContact.trim();
    const message = supportMessage.trim();
    if (!contact || message.length < 5 || supportBusy || booting) return;
    setError(null);
    setSupportNotice(null);
    setSupportBusy(true);
    try {
      let id = conversationId;
      if (!id) {
        id = (await createConversation()).conversation_id;
      }
      const response = await createAssistantSupportRequest(
        id,
        surface,
        contact,
        message,
      );
      setSupportNotice(response.message);
      setSupportOpen(false);
      setSupportMessage("");
      setMessages((current) => [
        ...current,
        {
          id: `support_${Date.now()}`,
          role: "assistant",
          content:
            "Запрос передан человеку из поддержки. Ответ придет по указанному контакту.",
          citations: [],
        },
      ]);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setSupportBusy(false);
    }
  }

  return (
    <>
      <Button
        type="button"
        className="fixed bottom-4 right-4 z-50 size-12 rounded-full bg-[#171915] text-[#d7ff46] shadow-xl hover:bg-[#2b2f27] sm:bottom-5 sm:right-5"
        size="icon"
        aria-label="Открыть AI-помощника"
        onClick={() => setOpen(true)}
      >
        <MessageSquareTextIcon className="size-5" />
      </Button>
      <Sheet open={open} onOpenChange={setOpen}>
        <SheetContent className="flex h-dvh w-full flex-col gap-0 p-0 sm:max-w-[480px]">
          <SheetHeader className="border-b px-4 py-3 text-left">
            <div className="flex items-center justify-between gap-3">
              <SheetTitle className="flex items-center gap-2 text-base">
                <span className="flex size-8 items-center justify-center rounded-md bg-[#171915] text-[#d7ff46]">
                  <BotIcon className="size-4" />
                </span>
                AI-помощник demo-platform
              </SheetTitle>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => setOpen(false)}
                aria-label="Закрыть"
              >
                <XIcon className="size-4" />
              </Button>
            </div>
          </SheetHeader>
          <div
            ref={listRef}
            className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-muted/20 px-4 py-4"
          >
            {booting && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2Icon className="size-4 animate-spin" />
                Подключаю помощника
              </div>
            )}
            {messages.map((message) => (
              <AssistantBubble
                key={message.id}
                message={message}
                onFeedback={(rating) =>
                  void sendAssistantFeedback(message.id, rating)
                }
              />
            ))}
          </div>
          {error && (
            <div className="border-t bg-destructive/8 px-4 py-2 text-sm text-destructive">
              {error}
            </div>
          )}
          {supportNotice && (
            <div className="border-t bg-emerald-50 px-4 py-2 text-sm text-emerald-800">
              {supportNotice}
            </div>
          )}
          {!!suggestedQuestions.length && !loading && (
            <div className="flex gap-2 overflow-x-auto border-t px-4 py-2">
              {suggestedQuestions.slice(0, 3).map((question) => (
                <Button
                  key={question}
                  type="button"
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  onClick={() => void submitMessage(question)}
                >
                  {question}
                </Button>
              ))}
            </div>
          )}
          <div className="border-t bg-background px-3 py-2">
            {supportOpen ? (
              <form className="space-y-2" onSubmit={submitSupportRequest}>
                <Input
                  value={supportContact}
                  onChange={(event) => setSupportContact(event.target.value)}
                  placeholder="Телефон, Telegram или email"
                  maxLength={240}
                />
                <Textarea
                  value={supportMessage}
                  onChange={(event) => setSupportMessage(event.target.value)}
                  placeholder="Что нужно передать поддержке?"
                  className="max-h-28 min-h-20 resize-none"
                  maxLength={2000}
                />
                <div className="flex items-center justify-between gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => setSupportOpen(false)}
                  >
                    Отмена
                  </Button>
                  <Button
                    type="submit"
                    size="sm"
                    disabled={
                      !supportContact.trim() ||
                      supportMessage.trim().length < 5 ||
                      supportBusy
                    }
                  >
                    {supportBusy ? (
                      <Loader2Icon className="mr-2 size-4 animate-spin" />
                    ) : (
                      <LifeBuoyIcon className="mr-2 size-4" />
                    )}
                    Отправить
                  </Button>
                </div>
              </form>
            ) : (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="w-full justify-start"
                onClick={() => {
                  setSupportOpen(true);
                  setSupportNotice(null);
                }}
              >
                <LifeBuoyIcon className="mr-2 size-4" />
                Передать вопрос человеку
              </Button>
            )}
          </div>
          <form
            className="flex items-end gap-2 border-t bg-background p-3"
            onSubmit={(event) => {
              event.preventDefault();
              void submitMessage();
            }}
          >
            <Textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="Спросите про модули, тарифы или запуск..."
              className="max-h-32 min-h-11 resize-none"
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void submitMessage();
                }
              }}
            />
            <Button
              type="submit"
              size="icon"
              disabled={!input.trim() || loading || booting}
              aria-label="Отправить"
            >
              {loading ? (
                <Loader2Icon className="size-4 animate-spin" />
              ) : (
                <SendIcon className="size-4" />
              )}
            </Button>
          </form>
        </SheetContent>
      </Sheet>
    </>
  );
}

function AssistantBubble({
  message,
  onFeedback,
}: {
  message: LocalMessage;
  onFeedback: (rating: "up" | "down") => void;
}) {
  const isUser = message.role === "user";
  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[86%] rounded-lg px-3 py-2 text-sm leading-6 shadow-sm",
          isUser
            ? "bg-primary text-primary-foreground"
            : "border bg-background text-foreground",
          message.pending && "text-muted-foreground",
        )}
      >
        <MarkdownContent content={message.content} />
        {!!message.citations.length && (
          <div className="mt-2 space-y-1 border-t pt-2">
            {message.citations.slice(0, 3).map((citation) => (
              <a
                key={citation.chunk_id}
                href={citation.url || "/help"}
                className="flex items-center gap-1 text-xs text-muted-foreground underline-offset-4 hover:underline"
              >
                {citation.title}
                <ExternalLinkIcon className="size-3" />
              </a>
            ))}
          </div>
        )}
        {!isUser && !message.pending && message.id !== "welcome" && (
          <div className="mt-2 flex gap-1">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7"
              aria-label="Ответ полезен"
              onClick={() => onFeedback("up")}
            >
              <ThumbsUpIcon className="size-3.5" />
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="size-7"
              aria-label="Ответ не помог"
              onClick={() => onFeedback("down")}
            >
              <ThumbsDownIcon className="size-3.5" />
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

function fromApiMessage(message: AssistantMessage): LocalMessage {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    citations: message.citations || [],
  };
}

function MarkdownContent({ content }: { content: string }) {
  const blocks = React.useMemo(() => markdownBlocks(content), [content]);

  return (
    <div className="space-y-2 whitespace-normal break-words">
      {blocks.map((block, index) => {
        if (block.type === "ordered-list") {
          return (
            <ol
              key={index}
              className="list-decimal space-y-1 pl-5 marker:text-muted-foreground"
            >
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{renderInlineMarkdown(item)}</li>
              ))}
            </ol>
          );
        }
        if (block.type === "unordered-list") {
          return (
            <ul
              key={index}
              className="list-disc space-y-1 pl-5 marker:text-muted-foreground"
            >
              {block.items.map((item, itemIndex) => (
                <li key={itemIndex}>{renderInlineMarkdown(item)}</li>
              ))}
            </ul>
          );
        }
        if (block.type === "paragraph") {
          return (
            <p key={index} className="m-0">
              {renderInlineMarkdown(block.text)}
            </p>
          );
        }
        return (
          <p key={index} className="m-0">
            {content}
          </p>
        );
      })}
    </div>
  );
}

type MarkdownBlock =
  | { type: "paragraph"; text: string }
  | { type: "ordered-list" | "unordered-list"; items: string[] };

function markdownBlocks(content: string): MarkdownBlock[] {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: MarkdownBlock[] = [];
  let paragraph: string[] = [];
  let listType: "ordered-list" | "unordered-list" | null = null;
  let listItems: string[] = [];

  function flushParagraph() {
    const text = paragraph.join(" ").trim();
    if (text) blocks.push({ type: "paragraph", text });
    paragraph = [];
  }

  function flushList() {
    if (listType && listItems.length) {
      blocks.push({ type: listType, items: listItems });
    }
    listType = null;
    listItems = [];
  }

  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) {
      flushParagraph();
      flushList();
      continue;
    }

    const ordered = line.match(/^\d+\.\s+(.+)$/);
    if (ordered) {
      flushParagraph();
      if (listType !== "ordered-list") flushList();
      listType = "ordered-list";
      listItems.push(ordered[1]);
      continue;
    }

    const unordered = line.match(/^[-*]\s+(.+)$/);
    if (unordered) {
      flushParagraph();
      if (listType !== "unordered-list") flushList();
      listType = "unordered-list";
      listItems.push(unordered[1]);
      continue;
    }

    flushList();
    paragraph.push(line);
  }

  flushParagraph();
  flushList();
  return blocks.length ? blocks : [{ type: "paragraph", text: content }];
}

function renderInlineMarkdown(text: string) {
  const parts: React.ReactNode[] = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const token = match[0];
    if (token.startsWith("**")) {
      parts.push(
        <strong key={`${match.index}-bold`} className="font-semibold">
          {token.slice(2, -2)}
        </strong>,
      );
    } else if (token.startsWith("`")) {
      parts.push(
        <code
          key={`${match.index}-code`}
          className="rounded bg-muted px-1 py-0.5 font-mono text-[0.92em]"
        >
          {token.slice(1, -1)}
        </code>,
      );
    } else {
      const link = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const href = link?.[2] || "#";
      parts.push(
        <a
          key={`${match.index}-link`}
          href={href}
          className="font-medium underline underline-offset-4"
          rel="noreferrer"
          target={href.startsWith("http") ? "_blank" : undefined}
        >
          {link?.[1] || href}
        </a>,
      );
    }
    lastIndex = match.index + token.length;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return parts;
}

function errorMessage(error: unknown) {
  return error instanceof Error
    ? error.message
    : "Помощник временно недоступен. Попробуйте еще раз.";
}

import {
  apiRequest,
  storedToken,
  storedWorkspaceId,
} from "@/lib/api";

const VISITOR_STORAGE_KEY = "demo-platform_assistant_visitor_id";
export const ASSISTANT_OPEN_EVENT = "demo-platform-assistant-open";

export type AssistantSurface = "landing" | "dashboard" | "help";

export type AssistantCitation = {
  title: string;
  url: string | null;
  chunk_id: string;
  source_id: string;
};

export type AssistantConversationResponse = {
  conversation_id: string;
  welcome_message: string;
};

export type AssistantMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: AssistantCitation[];
  created_at: string;
};

export type AssistantChatResponse = {
  message_id: string;
  answer: string;
  citations: AssistantCitation[];
  suggested_questions: string[];
};

export type AssistantSupportRequestResponse = {
  request_id: string;
  status: string;
  message: string;
};

export type AssistantOpenEventDetail = {
  support?: boolean;
  message?: string;
};

export function openAssistant(detail: AssistantOpenEventDetail = {}) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<AssistantOpenEventDetail>(ASSISTANT_OPEN_EVENT, {
      detail,
    }),
  );
}

export function openAssistantSupport(message?: string) {
  openAssistant({ support: true, message });
}

function randomId() {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `anon_${crypto.randomUUID()}`;
  }
  return `anon_${Date.now()}_${Math.random().toString(16).slice(2)}`;
}

export function assistantVisitorId() {
  if (typeof window === "undefined") return randomId();
  const existing = window.localStorage.getItem(VISITOR_STORAGE_KEY);
  if (existing) return existing;
  const next = randomId();
  window.localStorage.setItem(VISITOR_STORAGE_KEY, next);
  return next;
}

function authOptions() {
  const token = storedToken();
  const workspaceId = storedWorkspaceId();
  return token && workspaceId ? { token, workspaceId } : {};
}

export function createAssistantConversation(surface: AssistantSurface) {
  return apiRequest<AssistantConversationResponse>("/assistant/conversations", {
    method: "POST",
    body: {
      surface,
      initial_url:
        typeof window === "undefined"
          ? undefined
          : `${window.location.pathname}${window.location.search}`,
      visitor_id: assistantVisitorId(),
    },
    ...authOptions(),
  });
}

export function loadAssistantMessages(conversationId: string) {
  const query = new URLSearchParams({ visitor_id: assistantVisitorId() });
  return apiRequest<AssistantMessage[]>(
    `/assistant/conversations/${conversationId}/messages?${query.toString()}`,
    authOptions(),
  );
}

export function sendAssistantMessage(
  conversationId: string,
  surface: AssistantSurface,
  message: string,
) {
  return apiRequest<AssistantChatResponse>("/assistant/chat", {
    method: "POST",
    body: {
      conversation_id: conversationId,
      surface,
      message,
      page_url:
        typeof window === "undefined"
          ? undefined
          : `${window.location.pathname}${window.location.search}`,
      visitor_id: assistantVisitorId(),
    },
    ...authOptions(),
  });
}

export function sendAssistantFeedback(
  messageId: string,
  rating: "up" | "down",
  comment?: string,
) {
  return apiRequest<{ status: string }>(
    `/assistant/messages/${messageId}/feedback`,
    {
      method: "POST",
      body: { rating, comment },
      ...authOptions(),
    },
  );
}

export function createAssistantSupportRequest(
  conversationId: string,
  surface: AssistantSurface,
  contact: string,
  message: string,
) {
  return apiRequest<AssistantSupportRequestResponse>(
    "/assistant/support-requests",
    {
      method: "POST",
      body: {
        conversation_id: conversationId,
        surface,
        contact,
        message,
        page_url:
          typeof window === "undefined"
            ? undefined
            : `${window.location.pathname}${window.location.search}`,
        visitor_id: assistantVisitorId(),
      },
      ...authOptions(),
    },
  );
}

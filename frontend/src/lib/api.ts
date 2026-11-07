export const API_BASE_STORAGE_KEY = "tf_api_base";
export const TOKEN_STORAGE_KEY = "tf_token";
export const WORKSPACE_STORAGE_KEY = "tf_workspace_id";
export const USER_STORAGE_KEY = "tf_user";
export const WORKSPACE_DATA_STORAGE_KEY = "tf_workspace";
const SESSION_COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 7;

export type LoginResponse = {
  access_token: string | null;
  token_type: string;
  requires_email_verification?: boolean;
  message?: string | null;
  dev_verification_token?: string | null;
};

export type User = {
  id: string;
  email: string;
  full_name: string | null;
  status: string;
  email_verified_at?: string | null;
};

export type Workspace = {
  id: string;
  name: string;
  slug: string;
  status: string;
  plan_id: string | null;
};

export type ContactResponse = {
  status: string;
  message: string;
};

export type Entitlement = {
  id: string;
  workspace_id: string;
  feature_code: string;
  limit_value: number;
  used_value: number;
  reset_period: string;
  resets_at: string | null;
};

export type Plan = {
  id: string;
  code: string;
  name: string;
  status: string;
  monthly_price: number | string;
  yearly_price: number | string;
  currency: string;
  limits: Record<string, number>;
  features: Record<string, unknown>;
};

export type Subscription = {
  id: string;
  workspace_id: string;
  plan_id: string;
  provider: string;
  status: string;
  current_period_start: string | null;
  current_period_end: string | null;
  cancel_at_period_end: boolean;
};


export type CartProduct = {
  code: string;
  name: string;
  amount: number;
  currency: string;
  duration_days: number;
};

export type CartResponse = {
  user_id: string;
  workspace_id: string;
  items: string[];
  provider: "yookassa" | "nowpayments";
  products: CartProduct[];
  total: number;
  currency: string;
};

export type CheckoutResponse = {
  order_id: string;
  provider: string;
  status: string;
  checkout_url: string;
  amount: number;
  currency: string;
  product_code: string;
  product_name: string;
};

export type ModuleProduct = {
  code: string;
  name: string;
  description: string;
  monthly_price: number;
  yearly_price?: number;
  status?: string;
  module_codes?: string[];
};

export type AccessSummary = {
  plan: Plan | null;
  subscriptions: Subscription[];
  entitlements: Entitlement[];
  enabled_features: string[];
  feature_periods?: Record<
    string,
    {
      subscription_id: string;
      plan_id: string;
      plan_code: string | null;
      plan_name: string | null;
      product_code: string | null;
      current_period_start: string | null;
      current_period_end: string | null;
      remaining_seconds: number | null;
    }
  >;
  plan_period?: {
    subscription_id: string;
    plan_id: string;
    plan_code: string | null;
    plan_name: string | null;
    current_period_start: string | null;
    current_period_end: string | null;
    remaining_seconds: number | null;
  } | null;
  modules: ModuleProduct[];
  full_access_product: ModuleProduct;
  business_model: {
    currency: string;
    base_platform_fee: number;
    free_plan?: {
      code: string;
      name: string;
      monthly_price: number;
      description: string;
    };
    trial_plan?: {
      code: string;
      name: string;
      duration_days: number;
      monthly_price: number;
      description: string;
    };
    trial_credit?: {
      name: string;
      monthly_price: number;
      description: string;
    };
    self_serve_plans?: {
      code: string;
      name: string;
      monthly_price: number;
      yearly_price?: number;
      description: string;
    }[];
    recommended_bundle?: {
      name: string;
      monthly_price: number;
      yearly_price?: number;
      modules: string[];
    };
    pro_bundle?: {
      name: string;
      monthly_price: number;
      yearly_price?: number;
      modules: string[];
    };
    agency_bundle?: {
      name: string;
      monthly_price: number;
      modules: string[];
    };
    add_ons?: {
      code: string;
      name: string;
      monthly_price_from?: number;
      one_time_price_from?: number;
    }[];
    compliance_notes?: string[];
  };
};

export type TelegramSafetyState = {
  account_id: string;
  execution_enabled: boolean;
  manual_hold: boolean;
  reason: string | null;
  received_at: string | null;
  wait_seconds: number | null;
  wait_until: string | null;
  restriction_status: string;
  restriction_checked_at: string | null;
  operator_approved_at: string | null;
  first_seen_at: string | null;
  evidence: string | null;
  actor_id: string | null;
  scope: string | null;
  operation_id: string | null;
  outbound_attempt_limit: number;
  rpc_attempt_limit: number;
  diagnosis?: { label: string; meaning: string; spam_signal: string; category: string; raw_code: string; distinct_targets: number } | null;
  blocking_operation?: { error_code: string | null; method: string; target: string | null } | null;
  last_operation?: {
    id: string;
    method: string;
    outcome: string;
    error_code: string | null;
    created_at: string;
    message_id?: string | null;
  } | null;
};

export type TelegramAccount = {
  id: string;
  workspace_id: string;
  username: string | null;
  display_name: string | null;
  first_name?: string | null;
  last_name?: string | null;
  phone?: string | null;
  profile_photo_data_url?: string | null;
  status: string;
  proxy_id: string | null;
  proxy_required: boolean;
  health_score: number;
  warmup_score: number;
  warmup_status: string;
  origin?: "manual_phone_registered" | "market_session" | "tdata" | "sms_service_manual" | null;
  claimed_age_days?: number | null;
  real_age_days?: number | null;
  system_age_days?: number;
  trust_age_bonus?: number;
  incubator_mode?: boolean;
  incubator_until_at?: string | null;
  immortal_candidate?: boolean;
  owner_attested?: boolean;
  telegram_manual_hold?: boolean | null;
  telegram_safety_reason?: string | null;
  restriction_status?: string | null;
  restriction_label?: string | null;
  restriction_checked_at?: string | null;
  restriction_until_text?: string | null;
  restriction_until_at?: string | null;
  restriction_raw_text?: string | null;
  quality_score?: Record<string, unknown> | null;
  import_batch_id?: string | null;
  import_batch_size?: number | null;
  import_batch_at?: string | null;
  latest_trust?: AccountTrustSnapshot | null;
  created_at?: string;
  updated_at?: string;
};

export type AccountTrustFactor = {
  key: string;
  group: string;
  label: string;
  delta: number;
  severity: string;
  details?: Record<string, unknown>;
};

export type AccountTrustSnapshot = {
  id: string;
  workspace_id: string;
  telegram_account_id: string;
  score_100: number;
  score_10: number;
  label: string;
  confidence: string;
  safety_status: string;
  survival_7d: number;
  survival_30d: number;
  median_lifespan_days?: number | null;
  similar_sample_size: number;
  factor_scores: Record<string, number>;
  risk_factors: AccountTrustFactor[];
  positive_factors: AccountTrustFactor[];
  hard_blocks: AccountTrustFactor[];
  recommendations: {
    next_best_action: string;
    allowed_modules: string[];
    blocked_modules: string[];
    limits: Record<string, unknown>;
    reason?: string;
  };
  source_version: string;
  created_at: string;
};

export type AccountRiskSummary = {
  total: number;
  risk_levels: Record<string, number>;
  module_eligibility: Record<string, Record<string, number>>;
  restriction_statuses: Record<string, number>;
  hard_blocks: Record<string, number>;
};

export type AccountImportBatch = {
  batch_id: string;
  imported_at: string | null;
  declared_count: number | null;
  seller?: string | null;
  purchase_date?: string | null;
  declared_geo?: string | null;
  declared_age?: string | null;
  notes?: string | null;
  status?: string | null;
  account_count: number;
  clear_count: number;
  limited_count: number;
  blocked_count: number;
  unknown_count: number;
  invalid_count: number;
  quarantined_count: number;
  active_count: number;
  spam_block_rate: number;
  survival_rate: number;
  proxy_providers: string[];
  verdict: string;
};

export type RiskDecision = {
  id: string;
  workspace_id: string;
  account_id: string | null;
  module: string;
  decision: string;
  reasons: string[];
  limits: Record<string, unknown> | null;
  created_at: string;
};

export type AccountHealthEvent = {
  id: string;
  event_type: string;
  severity: string;
  message: string;
  raw_error: Record<string, unknown> | null;
  created_at: string;
};

export type OperationalEvent = {
  id: string;
  workspace_id: string | null;
  severity: "info" | "warning" | "error" | "critical";
  category: string;
  event_type: string;
  title: string;
  message: string;
  source: string | null;
  entity_type: string | null;
  entity_id: string | null;
  dedupe_key: string | null;
  payload: Record<string, unknown>;
  notified_at: string | null;
  resolved_at: string | null;
  created_at: string;
};

export type OperationsHealth = {
  status: "ok" | "warning" | "critical";
  generated_at: string;
  counters: {
    active_campaign_runs: number;
    stale_campaign_runs: number;
    events_24h: number;
    critical_events_24h: number;
    error_events_24h: number;
    telegram_accounts_active: number;
    telegram_accounts_problem: number;
  };
  checks: {
    key: string;
    label: string;
    status: "ok" | "warning" | "critical";
    message: string;
    updated_at: string | null;
    payload: Record<string, unknown>;
  }[];
  recent_events: OperationalEvent[];
};

export type TelegramAccountProfile = {
  account_id: string;
  username: string | null;
  first_name: string | null;
  last_name: string | null;
  display_name: string | null;
  bio: string | null;
  about: string | null;
  phone: string | null;
  photo_data_url: string | null;
  status: string;
  health_score: number;
  can_edit_security: boolean;
};

export type TelegramAccountProfilePhoto = {
  photo_id: string;
  date: string | null;
  photo_data_url: string | null;
  is_current: boolean;
};

export type TelegramAccountProfilePhotosResponse = {
  photos: TelegramAccountProfilePhoto[];
};

export type TelegramAccountLoginCodeResponse = {
  status: string;
  account_id: string;
  phone: string | null;
  login_code: string | null;
  message_date: string | null;
  message: string;
};

export type TelegramAccountCloneProfilePayload = {
  source_ref?: string | null;
  source_account_id?: string | null;
  copy_name: boolean;
  copy_bio: boolean;
  copy_photo: boolean;
  copy_stories: boolean;
  pin_stories?: boolean;
  delete_old_stories?: boolean;
  story_limit: number;
};

export type TelegramAccountCloneProfileResponse = {
  status: string;
  account_id: string;
  source_ref?: string | null;
  source_account_id?: string | null;
  mode: string;
  copied_fields: string[];
  skipped_fields: string[];
  warnings: string[];
  profile: TelegramAccountProfile;
};

export type Source = {
  id: string;
  workspace_id: string;
  folder_id?: string | null;
  type: string;
  username: string | null;
  title: string | null;
  url: string | null;
  comments_enabled: boolean;
  access_status: string;
  is_private?: boolean;
  is_join_request_required?: boolean;
  source_metadata: Record<string, unknown>;
  source_risk_level?: string;
  safe_for_new_accounts?: boolean;
  safe_for_trusted_accounts?: boolean;
  source_risk_reasons?: string[];
  source_recommended_action?: string | null;
};

export type SourceFolder = {
  id: string;
  workspace_id: string;
  name: string;
  description: string | null;
  color: string | null;
  sort_order: number;
  sources_count: number;
};

export type SourceBulkCreateResponse = {
  created: number;
  reused: number;
  items: {
    ref: string;
    status: string;
    source: Source;
  }[];
};

export type SourceSafetySummary = {
  total: number;
  safe: number;
  medium: number;
  high: number;
  critical: number;
  unknown: number;
  invalid: number;
  archived: number;
  blacklisted: number;
  comments_closed: number;
  private: number;
  join_request: number;
  write_forbidden: number;
  not_found: number;
  not_channel: number;
};

export type SourceBlacklistEntry = {
  id: string;
  workspace_id: string;
  source_id: string;
  source_ref: string | null;
  source_type: string | null;
  status: string;
  reason: string;
  error_code: string | null;
  error_message: string | null;
  campaign_id: string | null;
  account_id: string | null;
  blocked_by: string;
  first_blocked_at: string;
  last_seen_at: string;
  resolved_at: string | null;
  details: Record<string, unknown>;
  source: Source | null;
};

export type SourceBulkArchiveInvalidResponse = {
  status: string;
  archived: number;
  source_ids: string[];
};

export type ChannelMapCategory = {
  id: number;
  label: string;
  color: string;
  channel_count: number;
  parent_id: number | null;
};

export type ChannelMapChannel = {
  channel_id: string;
  username: string | null;
  title: string;
  participants_count: number;
  cluster_id: number | null;
  cluster_label: string | null;
  sub_cluster_id: number | null;
  sub_cluster_label: string | null;
  avatar_url: string | null;
  ref: string;
};

export type ChannelMapChannelsResponse = {
  total: number;
  atlas_total: number;
  limit: number;
  offset: number;
  has_more: boolean;
  items: ChannelMapChannel[];
};

export type ChannelMapSummary = {
  total_channels: number;
  macro_categories: number;
  micro_categories: number;
};

export type AuthorGender = "masculine" | "feminine" | "neutral";

export type CampaignConfig = Record<string, unknown> & {
  author_gender?: AuthorGender;
};

export type ContentFactoryContentConfig = Record<string, unknown> & {
  author_gender?: AuthorGender;
};

export type Campaign = {
  id: string;
  workspace_id: string;
  type: string;
  name: string;
  status: string;
  source_ids: string[];
  config: CampaignConfig;
  policy: Record<string, unknown>;
  sent_count?: number;
  active_run_status?: string | null;
  last_run_status?: string | null;
  last_error_code?: string | null;
  last_error_message?: string | null;
  invite_tracking?: Record<string, unknown> | null;
};

export type CampaignSafetyAccount = {
  account_id: string;
  account_label: string;
  module: string;
  status: string;
  reason: string | null;
  message: string | null;
  risk_level: string;
  risk_score: number;
  recommended_next_step: string | null;
};

export type CampaignSafety = {
  campaign_id: string;
  module: string;
  status: string;
  can_start: boolean;
  blocked_count: number;
  limited_count: number;
  accounts: CampaignSafetyAccount[];
  sources?: {
    source_id: string;
    source_label: string;
    status: string;
    reason: string | null;
    message: string | null;
    risk_level: string;
    recommended_next_step: string | null;
  }[];
};

export type CampaignDryRun = {
  campaign_id: string;
  campaign_type: string;
  can_start: boolean;
  accounts: Record<string, number>;
  sources: Record<string, number>;
  effective_policy: Record<string, unknown>;
  blocking_reasons: string[];
  warnings: string[];
  safety: CampaignSafety;
};

export type ContentFactory = {
  id: string;
  workspace_id: string;
  name: string;
  status: string;
  target_type: string;
  target_ref: string | null;
  publisher_account_id: string | null;
  mode: string;
  schedule_config: Record<string, unknown>;
  content_config: ContentFactoryContentConfig;
  image_config: Record<string, unknown>;
  quality_config: Record<string, unknown>;
  drafts_count: number;
  published_24h: number;
  published_7d: number;
  errors_count: number;
  next_run_at: string | null;
  last_successful_run_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ContentFactorySource = {
  id: string;
  factory_id: string;
  source_id: string | null;
  source_ref: string | null;
  source_type: string;
  enabled: boolean;
  weight: number;
  last_parsed_at: string | null;
  settings: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ContentFactoryKnowledgeItem = {
  id: string;
  factory_id: string;
  title: string;
  kind: string;
  content_text: string | null;
  file_path: string | null;
  metadata: Record<string, unknown>;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export type ContentFactoryRubric = {
  id: string;
  factory_id: string;
  name: string;
  description: string | null;
  target_share_percent: number;
  format_preset: string | null;
  prompt_override: string | null;
  enabled: boolean;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ContentPost = {
  id: string;
  factory_id: string;
  run_id: string | null;
  status: string;
  title: string | null;
  text: string;
  source_summary: string | null;
  source_refs: string[];
  image_prompt: string | null;
  image_attachment_id: string | null;
  telegram_message_id: string | null;
  telegram_link: string | null;
  scheduled_at: string | null;
  published_at: string | null;
  quality_score: number | null;
  quality_notes: string[];
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ContentFactoryRun = {
  id: string;
  factory_id: string;
  status: string;
  trigger_type: string;
  started_at: string | null;
  completed_at: string | null;
  error: string | null;
  metrics: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type ContentFactoryEvent = {
  id: string;
  factory_id: string;
  run_id: string | null;
  post_id: string | null;
  level: string;
  event_type: string;
  message: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type ContentFactorySourcePost = {
  id: string;
  source_id: string;
  source_title: string | null;
  source_username: string | null;
  source_url: string | null;
  tg_message_id: string;
  text: string | null;
  published_at: string | null;
  views_count: number;
  forwards_count: number;
  replies_count: number;
  created_at: string;
};

export type ContentFactoryDetail = ContentFactory & {
  sources: ContentFactorySource[];
  knowledge_items: ContentFactoryKnowledgeItem[];
  rubrics: ContentFactoryRubric[];
  recent_runs: ContentFactoryRun[];
  recent_events: ContentFactoryEvent[];
  source_posts: ContentFactorySourcePost[];
};

export type ContentFactoryRunResult = {
  run: ContentFactoryRun;
  post: ContentPost | null;
};

export type Proxy = {
  id: string;
  workspace_id: string;
  type: "http" | "socks5";
  host: string;
  port: number;
  status: string;
  country: string | null;
  provider: string | null;
  city_state?: string | null;
  proxy_class?: string | null;
  exclusivity?: string | null;
  sticky_until?: string | null;
  last_ip_detected?: string | null;
  last_country_detected?: string | null;
  telegram_connectivity_status?: string | null;
  max_accounts: number;
  note: string | null;
  account_count?: number;
  latest_latency_ms?: number | null;
  latest_telegram_latency_ms?: number | null;
  latest_checked_at?: string | null;
};

export type ProxyUsage = {
  proxy_id: string;
  accounts: {
    id: string;
    username: string | null;
    display_name: string | null;
    status: string;
  }[];
  account_count: number;
};

export type ProxyCheck = {
  id: string;
  proxy_id: string;
  status: string;
  latency_ms: number | null;
  ip_detected: string | null;
  telegram_ok: boolean | null;
  telegram_latency_ms: number | null;
  geo_country: string | null;
  error_code: string | null;
  error_message: string | null;
};

export type AccountMarketOffer = {
  id: string;
  seller_workspace_id: string;
  offer_type: "account" | "proxy" | "bundle";
  status: "draft" | "published" | "reserved" | "sold" | "archived";
  title: string;
  description: string | null;
  account_id: string | null;
  proxy_id: string | null;
  account_label: string | null;
  account_username: string | null;
  proxy_label: string | null;
  price_rub: number;
  currency: string;
  module_codes: string[];
  target_segments: string[];
  tags: string[];
  warmup_profile: string | null;
  warmup_days: number | null;
  warmup_score: number | null;
  health_score: number | null;
  quality_label: string | null;
  country: string | null;
  proxy_country: string | null;
  proxy_class: string | null;
  includes_proxy: boolean;
  ready_to_run: boolean;
  stock_count: number;
  sort_rank: number;
  metadata_json: Record<string, unknown>;
  can_manage: boolean;
  created_at: string;
  updated_at: string;
};

export type AccountMarketRequest = {
  id: string;
  workspace_id: string;
  seller_workspace_id: string | null;
  offer_id: string;
  requested_by_user_id: string;
  payment_order_id: string | null;
  status:
    | "new"
    | "payment_pending"
    | "paid"
    | "contacted"
    | "reserved"
    | "completed"
    | "cancelled";
  contact: string | null;
  message: string | null;
  quantity: number;
  module_code: string | null;
  provider: "yookassa" | "nowpayments" | null;
  amount_rub: number | null;
  admin_note: string | null;
  snapshot: Record<string, unknown>;
  paid_at: string | null;
  delivered_at: string | null;
  delivery_metadata: Record<string, unknown>;
  offer: AccountMarketOffer | null;
  created_at: string;
  updated_at: string;
};

export type ProxyBulkCheckItem = {
  raw: string;
  type: "http" | "socks5" | null;
  host: string | null;
  port: number | null;
  username: string | null;
  country: string | null;
  status: string;
  latency_ms: number | null;
  telegram_ok: boolean | null;
  telegram_latency_ms: number | null;
  error_code: string | null;
  error_message: string | null;
  proxy_id: string | null;
};

export type ProxyBulkCheckResponse = {
  total: number;
  parsed: number;
  valid: number;
  saved: number;
  items: ProxyBulkCheckItem[];
};

export type WarmupPlan = {
  id: string;
  name: string;
  status: string;
  duration_days: number;
  config: Record<string, unknown>;
  policy: Record<string, unknown>;
  account_ids: string[];
  scenario: string | null;
};

export type WarmupStep = {
  id: string;
  warmup_run_id: string;
  telegram_account_id: string;
  action_type: string;
  status: string;
  planned_at: string | null;
  executed_at: string | null;
  result: Record<string, unknown> | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type WarmupRun = {
  id: string;
  workspace_id: string;
  telegram_account_id: string;
  warmup_plan_id: string;
  status: string;
  current_day: number;
  score_before: number;
  score_current: number;
  started_at: string | null;
  completed_at: string | null;
  last_action_at: string | null;
  stop_reason: string | null;
  created_at: string;
  updated_at: string;
};

export type ApiOptions = {
  method?: string;
  token?: string;
  workspaceId?: string;
  body?: unknown;
  headers?: HeadersInit;
  timeoutMs?: number;
};

export class ApiError extends Error {
  status: number;
  payload: unknown;

  constructor(message: string, status: number, payload: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

function readLocalStorage(key: string) {
  try {
    return window.localStorage.getItem(key)?.trim() || "";
  } catch {
    return "";
  }
}

function readCookie(key: string) {
  const prefix = `${encodeURIComponent(key)}=`;
  const value =
    document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith(prefix))
      ?.slice(prefix.length) || "";
  try {
    return value ? decodeURIComponent(value) : "";
  } catch {
    return value;
  }
}

function writeCookie(key: string, value: string) {
  document.cookie = `${encodeURIComponent(key)}=${value}; max-age=${SESSION_COOKIE_MAX_AGE_SECONDS}; path=/; samesite=lax`;
}

function deleteCookie(key: string) {
  document.cookie = `${encodeURIComponent(key)}=; max-age=0; path=/; samesite=lax`;
}

function stripTrailingSlash(value: string) {
  return value.replace(/\/+$/, "");
}

const PRODUCTION_API_BASE = "https://demo.invalid/api/v1";
const API_FETCH_TIMEOUT_MS = 12_000;
const HEALTH_FETCH_TIMEOUT_MS = 6_000;

function isProductionFrontendHost(hostname: string) {
  return ["demo-platform.ru", "www.demo-platform.ru"].includes(hostname);
}

export function defaultApiBase() {
  const hostname = window.location.hostname;
  if (hostname === "localhost" || hostname === "127.0.0.1") {
    return "http://127.0.0.1:8000/api/v1";
  }
  if (isProductionFrontendHost(hostname)) {
    return PRODUCTION_API_BASE;
  }
  return `${window.location.origin}/demo-platform/api/v1`;
}

function fallbackApiBase() {
  const hostname = window.location.hostname;
  if (isProductionFrontendHost(hostname)) {
    return `${window.location.origin}/demo-platform/api/v1`;
  }
  return "";
}

function safeStoredApiBase() {
  const localOverride = readLocalStorage(API_BASE_STORAGE_KEY);
  if (!localOverride) return "";

  try {
    const storedUrl = new URL(localOverride, window.location.origin);
    const host = window.location.hostname;
    if (isProductionFrontendHost(host)) {
      const isProductionApi =
        storedUrl.protocol === "https:" &&
        (storedUrl.hostname === "demo.invalid" || storedUrl.origin === window.location.origin) &&
        storedUrl.pathname.replace(/\/+$/, "") === "/demo-platform/api/v1";
      if (!isProductionApi) {
        window.localStorage.removeItem(API_BASE_STORAGE_KEY);
        return "";
      }
    }
  } catch {
    window.localStorage.removeItem(API_BASE_STORAGE_KEY);
    return "";
  }

  return localOverride;
}

export function getApiBase() {
  const localOverride = safeStoredApiBase();
  const envOverride = String(import.meta.env.VITE_API_BASE || "").trim();
  return stripTrailingSlash(localOverride || envOverride || defaultApiBase());
}

export function getHealthBase() {
  return getApiBase().replace(/\/api\/v1$/, "");
}

function shouldRetryWithFallback(response: Response) {
  const currentBase = getApiBase();
  const backupBase = fallbackApiBase();
  if (!backupBase || currentBase === backupBase) return false;
  try {
    const currentUrl = new URL(currentBase);
    if (
      currentUrl.protocol !== "https:" ||
      currentUrl.hostname !== "demo.invalid" ||
      currentUrl.pathname.replace(/\/+$/, "") === "/demo-platform/api/v1"
    ) {
      return false;
    }
  } catch {
    return false;
  }
  const contentType = response.headers.get("content-type") || "";
  return response.status === 404 && contentType.includes("text/html");
}

async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs = API_FETCH_TIMEOUT_MS,
) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(input, {
      ...init,
      signal: init.signal ? AbortSignal.any([init.signal, controller.signal]) : controller.signal,
    });
    // Keep the deadline active until the entire body arrives, not only headers.
    const bytes = await response.arrayBuffer();
    const noBody = [204, 205, 304].includes(response.status);
    return new Response(noBody ? null : bytes, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  } finally {
    window.clearTimeout(timeout);
  }
}

async function fetchWithApiFallback(
  path: string,
  init?: RequestInit,
  timeoutMs = API_FETCH_TIMEOUT_MS,
) {
  let response: Response;
  const base = getApiBase();
  const readOnly = ["GET", "HEAD"].includes((init?.method || "GET").toUpperCase());
  const firstTimeout = readOnly && isProductionFrontendHost(window.location.hostname)
    && base === PRODUCTION_API_BASE ? Math.min(timeoutMs, 4_000) : timeoutMs;
  try {
    response = await fetchWithTimeout(`${base}${path}`, init, firstTimeout);
  } catch (error) {
    const backupBase = fallbackApiBase();
    if (readOnly && backupBase && base !== backupBase) {
      window.localStorage.setItem(API_BASE_STORAGE_KEY, backupBase);
      return fetchWithTimeout(`${backupBase}${path}`, init, timeoutMs);
    }
    throw error;
  }

  if (readOnly && shouldRetryWithFallback(response)) {
    const backupBase = fallbackApiBase();
    window.localStorage.setItem(API_BASE_STORAGE_KEY, backupBase);
    return fetchWithTimeout(`${backupBase}${path}`, init, timeoutMs);
  }

  return response;
}

export async function apiRequest<T>(path: string, options: ApiOptions = {}) {
  const headers = new Headers(options.headers);
  const isFormData = options.body instanceof FormData;
  const body: BodyInit | undefined = isFormData
    ? (options.body as FormData)
    : options.body === undefined
      ? undefined
      : JSON.stringify(options.body);

  if (options.token) {
    headers.set("Authorization", `Bearer ${options.token}`);
  }
  if (options.workspaceId) {
    headers.set("X-Workspace-Id", options.workspaceId);
  }
  if (
    options.body !== undefined &&
    !isFormData &&
    !headers.has("Content-Type")
  ) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetchWithApiFallback(path, {
    method: options.method || "GET",
    headers,
    body,
  }, options.timeoutMs);

  return parseApiResponse<T>(response);
}

export async function apiBlobRequest(path: string, options: ApiOptions = {}) {
  const headers = new Headers(options.headers);
  if (options.token) {
    headers.set("Authorization", `Bearer ${options.token}`);
  }
  if (options.workspaceId) {
    headers.set("X-Workspace-Id", options.workspaceId);
  }
  const response = await fetchWithApiFallback(path, {
    method: options.method || "GET",
    headers,
  });
  if (!response.ok) {
    await parseApiResponse(response);
  }
  return response.blob();
}

export async function parseApiResponse<T>(response: Response) {
  const raw = await response.text();
  let payload: unknown;
  try {
    payload = raw ? JSON.parse(raw) : null;
  } catch {
    payload = raw || null;
  }

  if (!response.ok) {
    throw new ApiError(
      errorMessage(payload, response),
      response.status,
      payload,
    );
  }

  return payload as T;
}

export async function loginRequest(email: string, password: string) {
  const body = new URLSearchParams();
  body.set("username", email);
  body.set("password", password);

  const response = await fetchWithApiFallback("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });

  return parseApiResponse<LoginResponse>(response);
}

export async function registerRequest(
  email: string,
  password: string,
  workspaceName: string,
) {
  const response = await fetchWithApiFallback("/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email,
      password,
      workspace_name: workspaceName,
    }),
  });

  return parseApiResponse<LoginResponse>(response);
}

export async function verifyEmailRequest(token: string) {
  return apiRequest<LoginResponse>("/auth/verify-email", {
    method: "POST",
    body: { token },
  });
}

export async function resendVerificationRequest(email: string) {
  return apiRequest<LoginResponse>("/auth/resend-verification", {
    method: "POST",
    body: { email },
  });
}

export async function googleStartRequest() {
  return apiRequest<{ authorization_url: string; state: string }>(
    "/auth/google/start",
  );
}

export async function googleCallbackRequest(code: string, state: string) {
  return apiRequest<LoginResponse>("/auth/google/callback", {
    method: "POST",
    body: { code, state },
  });
}

export async function checkHealth() {
  let response: Response;
  const primaryBase = getApiBase();
  try {
    response = await fetchWithTimeout(`${getHealthBase()}/health`, {}, HEALTH_FETCH_TIMEOUT_MS);
  } catch (error) {
    const backupBase = fallbackApiBase();
    if (!backupBase || primaryBase === backupBase) throw error;
    window.localStorage.setItem(API_BASE_STORAGE_KEY, backupBase);
    response = await fetchWithTimeout(
      `${backupBase.replace(/\/api\/v1$/, "")}/health`, {}, HEALTH_FETCH_TIMEOUT_MS,
    );
  }
  if (shouldRetryWithFallback(response)) {
    const backupBase = fallbackApiBase();
    window.localStorage.setItem(API_BASE_STORAGE_KEY, backupBase);
    response = await fetchWithTimeout(
      `${backupBase.replace(/\/api\/v1$/, "")}/health`,
      {},
      HEALTH_FETCH_TIMEOUT_MS,
    );
  }
  if (!response.ok) {
    throw new ApiError("health failed", response.status, null);
  }
  return parseApiResponse<{ status: string; service: string; env: string }>(
    response,
  );
}

export function clearSessionStorage() {
  window.localStorage.removeItem(TOKEN_STORAGE_KEY);
  window.localStorage.removeItem(WORKSPACE_STORAGE_KEY);
  window.localStorage.removeItem(USER_STORAGE_KEY);
  window.localStorage.removeItem(WORKSPACE_DATA_STORAGE_KEY);
  window.localStorage.removeItem("tf_email");
  deleteCookie(TOKEN_STORAGE_KEY);
  deleteCookie(WORKSPACE_STORAGE_KEY);
  deleteCookie(USER_STORAGE_KEY);
  deleteCookie(WORKSPACE_DATA_STORAGE_KEY);
  deleteCookie("tf_email");
}

export function storedToken() {
  return readLocalStorage(TOKEN_STORAGE_KEY) || readCookie(TOKEN_STORAGE_KEY);
}

export function storedWorkspaceId() {
  return (
    readLocalStorage(WORKSPACE_STORAGE_KEY) || readCookie(WORKSPACE_STORAGE_KEY)
  );
}

export function persistSession(
  token: string,
  workspace: Workspace,
  user: User,
) {
  const workspaceJson = JSON.stringify(workspace);
  const userJson = JSON.stringify(user);
  window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
  window.localStorage.setItem(WORKSPACE_STORAGE_KEY, workspace.id);
  window.localStorage.setItem(WORKSPACE_DATA_STORAGE_KEY, workspaceJson);
  window.localStorage.setItem(USER_STORAGE_KEY, userJson);
  window.localStorage.setItem("tf_email", user.email);
  writeCookie(TOKEN_STORAGE_KEY, encodeURIComponent(token));
  writeCookie(WORKSPACE_STORAGE_KEY, encodeURIComponent(workspace.id));
  writeCookie(WORKSPACE_DATA_STORAGE_KEY, encodeURIComponent(workspaceJson));
  writeCookie(USER_STORAGE_KEY, encodeURIComponent(userJson));
  writeCookie("tf_email", encodeURIComponent(user.email));
}


export async function getCartRequest(token: string, workspaceId: string) {
  return apiRequest<CartResponse>("/billing/cart", { token, workspaceId });
}

export async function updateCartRequest(
  token: string,
  workspaceId: string,
  items: string[],
  provider: "yookassa" | "nowpayments" = "yookassa",
) {
  return apiRequest<CartResponse>("/billing/cart", {
    method: "PUT",
    token,
    workspaceId,
    body: { items, provider },
  });
}

export async function checkoutCartRequest(
  token: string,
  workspaceId: string,
  provider: "yookassa" | "nowpayments" = "yookassa",
) {
  return apiRequest<CheckoutResponse>("/billing/cart/checkout", {
    method: "POST",
    token,
    workspaceId,
    body: { provider },
  });
}

function errorMessage(payload: unknown, response: Response) {
  if (typeof payload === "object" && payload !== null && "detail" in payload) {
    const detail = (payload as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return detail;
    }
    return JSON.stringify(detail);
  }
  if (typeof payload === "string" && payload) {
    return payload;
  }
  return `${response.status} ${response.statusText}`;
}

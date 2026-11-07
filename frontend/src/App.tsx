import * as React from "react"
import { Navigate, Route, Routes, useLocation, useParams } from "react-router-dom"
import { Loader2Icon } from "lucide-react"

import { trackPageView } from "@/lib/metrika"
import { HelpArticlePage, HelpCenterPage } from "@/pages/help"
import {
  AboutPage,
  BlogArticlePage,
  BlogIndexPage,
  blogArticles,
  CheckoutPage,
  ContactPage,
  FaqPage,
  HomePage,
  LegalPage,
  NotFoundPage,
  PlatformPage,
  PricingPage,
  SeoLandingPage,
  seoLandingPages,
  SitemapPage,
  UseCasesPage,
} from "@/pages/marketing"

const seoLandingSlugs = Object.keys(seoLandingPages)
const blogSlugs = Object.keys(blogArticles)

const DashboardApp = React.lazy(() => import("@/DashboardApp"))

function AppLoading() {
  return (
    <main className="grid min-h-svh place-items-center bg-muted/30 p-6">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2Icon className="size-4 animate-spin" />
        Загружаю кабинет
      </div>
    </main>
  )
}

function DashboardRoute() {
  return (
    <React.Suspense fallback={<AppLoading />}>
      <DashboardApp />
    </React.Suspense>
  )
}

const shortCampaignLinks: Record<string, string> = {
  "01": "account_01",
  "02": "account_02",
  "03": "account_03",
  "04": "account_04",
  "05": "account_05",
  "06": "account_06",
  "07": "account_07",
  "08": "account_08",
  "09": "account_09",
  "10": "account_10",
}

function ShortCampaignRedirect() {
  const { code } = useParams()
  const content = code ? shortCampaignLinks[code] : null

  if (!content) {
    return <Navigate to="/" replace />
  }

  return (
    <Navigate
      to={`/?utm_source=telegram&utm_medium=neurocommenting&utm_campaign=nc_launch_2026_05&utm_content=${content}`}
      replace
    />
  )
}

export default function App() {
  const location = useLocation()

  React.useEffect(() => {
    trackPageView(`${location.pathname}${location.search}${location.hash}`)
  }, [location])

  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/platform" element={<PlatformPage />} />
      <Route path="/about" element={<AboutPage />} />
      <Route path="/use-cases" element={<UseCasesPage />} />
      {seoLandingSlugs.map((slug) => (
        <Route
          key={slug}
          path={`/${slug}`}
          element={<SeoLandingPage pageKey={slug} />}
        />
      ))}
      <Route path="/pricing" element={<PricingPage />} />
      <Route path="/help" element={<HelpCenterPage />} />
      <Route path="/help/:slug" element={<HelpArticlePage />} />
      <Route path="/blog" element={<BlogIndexPage />} />
      {blogSlugs.map((slug) => (
        <Route
          key={slug}
          path={`/blog/${slug}`}
          element={<BlogArticlePage articleKey={slug} />}
        />
      ))}
      <Route path="/cart" element={<CheckoutPage />} />
      <Route path="/checkout" element={<CheckoutPage />} />
      <Route path="/faq" element={<FaqPage />} />
      <Route path="/contact" element={<ContactPage />} />
      <Route path="/sitemap" element={<SitemapPage />} />
      <Route path="/legal/privacy" element={<LegalPage title="Политика конфиденциальности" />} />
      <Route path="/legal/offer" element={<LegalPage title="Оферта" />} />
      <Route path="/legal/terms" element={<LegalPage title="Пользовательское соглашение" />} />
      <Route path="/legal/payment" element={<LegalPage title="Оплата и возвраты" />} />
      <Route path="/nc/:code" element={<ShortCampaignRedirect />} />
      <Route path="/login" element={<DashboardRoute />} />
      <Route path="/signup" element={<DashboardRoute />} />
      <Route path="/dashboard/*" element={<DashboardRoute />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}

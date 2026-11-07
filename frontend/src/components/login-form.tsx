import * as React from "react"
import { MessageSquareTextIcon } from "lucide-react"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldSeparator,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { trackGoal } from "@/lib/metrika"
import { cn } from "@/lib/utils"

type AuthMode = "login" | "signup" | "verify"

function GoogleIcon() {
  return (
    <svg aria-hidden="true" className="size-4" viewBox="0 0 24 24">
      <path
        d="M21.35 11.1H12v2.95h5.35c-.23 1.5-1.62 4.4-5.35 4.4A6.43 6.43 0 0 1 5.5 12 6.43 6.43 0 0 1 12 5.55c1.84 0 3.07.78 3.77 1.45l2.57-2.47C16.69 2.99 14.56 2.05 12 2.05 6.5 2.05 2.05 6.5 2.05 12S6.5 21.95 12 21.95c5.74 0 9.55-4.03 9.55-9.72 0-.65-.07-1.15-.2-1.13Z"
        fill="currentColor"
      />
    </svg>
  )
}

function readableAuthError(error: string | null | undefined) {
  if (!error) return null
  if (/invalid authentication credentials/i.test(error)) return "Неверный email или пароль"
  return error
}

export function LoginForm({
  className,
  initialMode = "login",
  onLogin,
  onRegister,
  onVerifyEmail,
  onResendVerification,
  onGoogleLogin,
  isBusy,
  error,
  notice,
  ...props
}: React.ComponentProps<"div"> & {
  onLogin: (email: string, password: string) => Promise<void>
  onRegister: (email: string, password: string, workspaceName: string) => Promise<void>
  onVerifyEmail: (token: string) => Promise<void>
  onResendVerification: (email: string) => Promise<void>
  onGoogleLogin: () => Promise<void>
  initialMode?: AuthMode
  isBusy?: boolean
  error?: string | null
  notice?: string | null
}) {
  const [mode, setMode] = React.useState<AuthMode>(initialMode)
  const [lastEmail, setLastEmail] = React.useState("")
  const [formError, setFormError] = React.useState<string | null>(null)
  const visibleError = formError || readableAuthError(error)

  React.useEffect(() => {
    setMode(initialMode)
    setFormError(null)
  }, [initialMode])

  React.useEffect(() => {
    if (notice && mode === "signup") {
      setMode("verify")
    }
  }, [mode, notice])

  async function handleLoginSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)
    const form = new FormData(event.currentTarget)
    const email = String(form.get("email") || "").trim()
    const password = String(form.get("password") || "")
    setLastEmail(email)
    await onLogin(email, password)
  }

  async function handleSignupSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)
    const form = new FormData(event.currentTarget)
    const email = String(form.get("email") || "").trim()
    const password = String(form.get("password") || "")
    const confirmPassword = String(form.get("confirmPassword") || "")
    const workspaceName = String(form.get("workspaceName") || "").trim()

    if (password !== confirmPassword) {
      setFormError("Пароли не совпадают")
      return
    }

    setLastEmail(email)
    await onRegister(email, password, workspaceName || "demo-platform")
  }

  async function handleVerifySubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError(null)
    const form = new FormData(event.currentTarget)
    await onVerifyEmail(String(form.get("verificationToken") || "").trim())
  }

  function handleModeChange(nextMode: AuthMode) {
    setMode(nextMode)
    setFormError(null)
  }

  return (
    <div className={cn("flex flex-col gap-6", className)} {...props}>
      <Card>
        <CardHeader className="text-center">
          <div className="mx-auto mb-2 flex size-10 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <MessageSquareTextIcon className="size-5" />
          </div>
          <CardTitle className="text-2xl">
            demo<span className="text-[#008f8b]">platform</span>
          </CardTitle>
          <CardDescription>
            {mode === "signup"
              ? "Создайте новый кабинет"
              : mode === "verify"
                ? "Подтвердите email"
                : "Войдите в рабочий кабинет"}
          </CardDescription>
        </CardHeader>
        <CardContent>
          {mode === "login" && (
            <form onSubmit={(event) => void handleLoginSubmit(event)}>
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="login-email">Email</FieldLabel>
                  <Input id="login-email" name="email" type="email" placeholder="admin@example.invalid" autoComplete="email" required />
                </Field>
                <Field>
                  <FieldLabel htmlFor="login-password">Пароль</FieldLabel>
                  <Input id="login-password" name="password" type="password" autoComplete="current-password" required />
                </Field>
                {visibleError && (
                  <FieldDescription className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-destructive">
                    {visibleError}
                  </FieldDescription>
                )}
                {notice && (
                  <FieldDescription className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-emerald-700">
                    {notice}
                  </FieldDescription>
                )}
                <Field>
                  <Button type="submit" disabled={isBusy}>
                    {isBusy ? "Входим..." : "Войти"}
                  </Button>
                </Field>
                <FieldSeparator>или</FieldSeparator>
                <Field>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={isBusy}
                    onClick={() => {
                      trackGoal("google_auth_click", { mode: "login" })
                      void onGoogleLogin()
                    }}
                  >
                    <GoogleIcon />
                    Войти через Google
                  </Button>
                </Field>
                <FieldDescription className="text-center">
                  Нет кабинета?{" "}
                  <button type="button" className="font-medium text-foreground underline-offset-4 hover:underline" onClick={() => handleModeChange("signup")}>
                    Зарегистрироваться
                  </button>
                </FieldDescription>
                <FieldDescription className="text-center">
                  <button type="button" className="underline-offset-4 hover:text-foreground hover:underline" onClick={() => handleModeChange("verify")}>
                    Подтвердить email
                  </button>
                </FieldDescription>
              </FieldGroup>
            </form>
          )}

          {mode === "signup" && (
            <form onSubmit={(event) => void handleSignupSubmit(event)}>
              <FieldGroup>
                <Field>
                  <FieldLabel htmlFor="signup-workspace">Название кабинета</FieldLabel>
                  <Input id="signup-workspace" name="workspaceName" type="text" placeholder="Мой кабинет" autoComplete="organization" minLength={2} />
                </Field>
                <Field>
                  <FieldLabel htmlFor="signup-email">Email</FieldLabel>
                  <Input id="signup-email" name="email" type="email" placeholder="admin@example.invalid" autoComplete="email" required />
                </Field>
                <Field>
                  <FieldLabel htmlFor="signup-password">Пароль</FieldLabel>
                  <Input id="signup-password" name="password" type="password" autoComplete="new-password" minLength={8} required />
                </Field>
                <Field>
                  <FieldLabel htmlFor="signup-confirm-password">Повторите пароль</FieldLabel>
                  <Input id="signup-confirm-password" name="confirmPassword" type="password" autoComplete="new-password" minLength={8} required />
                </Field>
                {visibleError && (
                  <FieldDescription className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-destructive">
                    {visibleError}
                  </FieldDescription>
                )}
                {notice && (
                  <FieldDescription className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-emerald-700">
                    {notice}
                  </FieldDescription>
                )}
                <Field>
                  <Button type="submit" disabled={isBusy}>
                    {isBusy ? "Создаем..." : "Зарегистрироваться"}
                  </Button>
                </Field>
                <FieldSeparator>или</FieldSeparator>
                <Field>
                  <Button
                    type="button"
                    variant="outline"
                    disabled={isBusy}
                    onClick={() => {
                      trackGoal("google_auth_click", { mode: "signup" })
                      void onGoogleLogin()
                    }}
                  >
                    <GoogleIcon />
                    Зарегистрироваться через Google
                  </Button>
                </Field>
                <FieldDescription className="text-center">
                  Уже есть кабинет?{" "}
                  <button type="button" className="font-medium text-foreground underline-offset-4 hover:underline" onClick={() => handleModeChange("login")}>
                    Войти
                  </button>
                </FieldDescription>
              </FieldGroup>
            </form>
          )}

          {mode === "verify" && (
            <form onSubmit={(event) => void handleVerifySubmit(event)}>
              <FieldGroup>
                {notice && (
                  <FieldDescription className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-emerald-700">
                    {notice}
                  </FieldDescription>
                )}
                {visibleError && (
                  <FieldDescription className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-destructive">
                    {visibleError}
                  </FieldDescription>
                )}
                <Field>
                  <FieldLabel htmlFor="verify-token">Код подтверждения</FieldLabel>
                  <Input id="verify-token" name="verificationToken" placeholder="Вставьте токен из письма" autoComplete="one-time-code" required />
                </Field>
                <Field>
                  <Button type="submit" disabled={isBusy}>
                    {isBusy ? "Проверяем..." : "Подтвердить почту"}
                  </Button>
                </Field>
                <FieldSeparator>письмо не пришло?</FieldSeparator>
                <Field>
                  <Input
                    aria-label="Email для повторной отправки"
                    type="email"
                    placeholder="admin@example.invalid"
                    defaultValue={lastEmail}
                    onChange={(event) => setLastEmail(event.target.value)}
                  />
                  <Button type="button" variant="outline" disabled={isBusy} onClick={() => void onResendVerification(lastEmail)}>
                    Отправить еще раз
                  </Button>
                </Field>
                <FieldDescription className="text-center">
                  <button type="button" className="font-medium text-foreground underline-offset-4 hover:underline" onClick={() => handleModeChange("login")}>
                    Вернуться ко входу
                  </button>
                </FieldDescription>
              </FieldGroup>
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

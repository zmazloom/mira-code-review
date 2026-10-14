import { CheckCircle2, Loader2 } from "lucide-react"
import { useEffect, useState } from "react"
import { ModelCombobox, type ModelOption } from "@/components/model-combobox"

import { Button } from "@/components/ui/button"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { fetchJson, postJson } from "@/lib/api/http"
import { useAuth } from "@/lib/auth"

type ReviewJob = {
  id: string
  platform: "github" | "gitlab"
  url: string
  model: string | null
  status: "reviewing" | "done" | "skipped" | "failed"
  message: string
}

function errorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  try {
    const body = JSON.parse(message.replace(/^API error \d+: /, ""))
    if (typeof body.detail === "string") return body.detail
  } catch { /* Use the original message for network errors. */ }
  return message
}

export function ManualReviewPage() {
  const { user } = useAuth()
  const storageKey = `mira-manual-review-${user?.id}`
  const [platform, setPlatform] = useState<"github" | "gitlab">("github")
  const [url, setUrl] = useState("")
  const [model, setModel] = useState("")
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([])
  const [defaultModel, setDefaultModel] = useState("")
  const [modelsLoading, setModelsLoading] = useState(true)
  const [job, setJob] = useState<ReviewJob | null>(null)
  const [pendingId, setPendingId] = useState<string | null>(() => sessionStorage.getItem(storageKey))
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchJson<{ default: string; options: ModelOption[] }>("/api/manual-reviews/models")
      .then((data) => {
        if (cancelled) return
        setModelOptions(data.options)
        setDefaultModel(data.default)
      })
      .catch((error) => { if (!cancelled) setError(errorMessage(error)) })
      .finally(() => { if (!cancelled) setModelsLoading(false) })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!pendingId) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    let failures = 0
    const poll = async () => {
      try {
        const result = await fetchJson<ReviewJob>(`/api/manual-reviews/${pendingId}`)
        if (cancelled) return
        failures = 0
        setJob(result)
        setPlatform(result.platform)
        setUrl(result.url)
        setModel(result.model || "")
        setError(null)
        if (result.status !== "reviewing") {
          sessionStorage.removeItem(storageKey)
          setPendingId(null)
          return
        }
      } catch (error) {
        if (cancelled) return
        setError(errorMessage(error))
        if (error instanceof Error && error.message.startsWith("API error 404:")) {
          sessionStorage.removeItem(storageKey)
          setPendingId(null)
          setJob(null)
          return
        }
        failures += 1
      }
      timer = setTimeout(poll, Math.min(2000 * (failures + 1), 10000))
    }
    void poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [pendingId, storageKey])

  const busy = starting || pendingId !== null
  const start = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (busy) return
    setStarting(true)
    setError(null)
    setJob(null)
    try {
      const result = await postJson<ReviewJob>("/api/manual-reviews", {
        platform, url: url.trim(), model: model.trim() || defaultModel || null,
      })
      setJob(result)
      sessionStorage.setItem(storageKey, result.id)
      setPendingId(result.id)
    } catch (error) {
      setError(errorMessage(error))
    } finally {
      setStarting(false)
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">Start review</h1>
        <p className="text-sm text-muted-foreground">Review requests whose title starts with “Notif MR” and post the findings.</p>
      </div>
      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle>Review a request</CardTitle>
          <CardDescription>Use a repository accessible to Mira’s configured platform token.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={start} className="space-y-5">
            <div className="space-y-2">
              <label htmlFor="review-model" className="text-sm font-medium">LLM model</label>
              <ModelCombobox id="review-model" value={model} onChange={setModel}
                options={modelOptions} configModel={defaultModel || undefined} disabled={busy || modelsLoading} />
              <p className="text-sm text-muted-foreground">
                {modelsLoading ? "Loading models…" : "Choose a model from your configured LLM service, or enter a model ID."}
              </p>
            </div>
            <div className="space-y-2">
              <label htmlFor="review-platform" className="text-sm font-medium">Platform</label>
              <Select value={platform} onValueChange={(value) => setPlatform(value as "github" | "gitlab")} disabled={busy}>
                <SelectTrigger id="review-platform"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="github">GitHub</SelectItem>
                  <SelectItem value="gitlab">GitLab</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label htmlFor="review-url" className="text-sm font-medium">{platform === "github" ? "Pull request link" : "Merge request link"}</label>
              <Input id="review-url" type="url" required maxLength={2048} value={url} onChange={(event) => setUrl(event.target.value)} disabled={busy}
                placeholder={platform === "github" ? "https://github.com/owner/repo/pull/123" : "https://gitlab.com/group/project/-/merge_requests/123"} />
            </div>
            <p className="text-sm text-muted-foreground">The title must start with “Notif MR” (any letter case). Matching requests receive a posted summary and inline findings; other requests are skipped.</p>
            <Button type="submit" disabled={busy || modelsLoading || !url.trim()}>
              {busy && <Loader2 className="size-4 animate-spin" />}
              {busy ? "Reviewing…" : "Start review"}
            </Button>
          </form>
          <div aria-live="polite" role="status" className="mt-5 space-y-2 text-sm">
            {job && <p className="flex items-center gap-2">
              {job.status === "done" && <CheckCircle2 className="size-4 text-green-600" />}
              {job.message}
            </p>}
            {error && <p role="alert" className="text-destructive">{error}</p>}
            {job?.status === "failed" && <p className="text-muted-foreground">Check the request for comments before retrying.</p>}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

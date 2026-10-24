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
  mode?: "TASK_AWARE"
  phase?: string
  publishing_status?: string
  result?: {
    reviewed_commit_sha?: string
    partial?: boolean
    requirements: { id: string; description: string; status: string; uncertainty: string; evidence: { path: string; start_line: number; end_line: number; quote: string }[] }[]
    findings?: { path: string; line: number; title: string; body: string; severity: string; location: string }[]
    limitations: string[]
    coverage: { retrieved_files: string[] }
  } | null
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
  const [mode, setMode] = useState("STANDARD")
  const [taskPrompt, setTaskPrompt] = useState("")
  const [depth, setDepth] = useState("balanced")
  const [postComments, setPostComments] = useState(false)
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
        const taskAware = pendingId.startsWith("task:")
        const endpoint = taskAware ? `/api/reviews/task-aware/${pendingId.slice(5)}` : `/api/manual-reviews/${pendingId}`
        const result = await fetchJson<ReviewJob>(endpoint)
        if (cancelled) return
        failures = 0
        setJob(result)
        setMode(taskAware ? "TASK_AWARE" : "STANDARD")
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
      const taskAware = mode === "TASK_AWARE"
      const result = await postJson<ReviewJob>(taskAware ? "/api/reviews/task-aware" : "/api/manual-reviews", taskAware ? {
        pr_url: url.trim(), task_prompt: taskPrompt.trim(), model: model.trim() || null,
        review_options: { depth, post_comments: postComments },
      } : { platform, url: url.trim(), model: model.trim() || defaultModel || null })
      setJob(result)
      const storedId = taskAware ? `task:${result.id}` : result.id
      sessionStorage.setItem(storageKey, storedId)
      setPendingId(storedId)
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
        <p className="text-sm text-muted-foreground">Review a request using standard checks or verify its implementation against a task.</p>
      </div>
      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle>Review a request</CardTitle>
          <CardDescription>Use a repository accessible to Mira’s configured platform token.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={start} className="space-y-5">
            <div className="space-y-2">
              <label htmlFor="review-mode" className="text-sm font-medium">Review mode</label>
              <Select value={mode} onValueChange={setMode} disabled={busy}>
                <SelectTrigger id="review-mode"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="STANDARD">Standard review</SelectItem>
                  <SelectItem value="TASK_AWARE">Task-aware repository review</SelectItem>
                </SelectContent>
              </Select>
            </div>
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
            {mode === "TASK_AWARE" ? <>
              <div className="space-y-2">
                <label htmlFor="task-prompt" className="text-sm font-medium">Implementation task</label>
                <textarea id="task-prompt" required minLength={3} maxLength={16000} rows={5} value={taskPrompt}
                  onChange={(event) => setTaskPrompt(event.target.value)} disabled={busy}
                  className="w-full rounded-md border bg-transparent p-3 text-sm"
                  placeholder="Describe the functionality this request should implement." />
              </div>
              <div className="space-y-2">
                <label htmlFor="review-depth" className="text-sm font-medium">Investigation depth</label>
                <Select value={depth} onValueChange={setDepth} disabled={busy}>
                  <SelectTrigger id="review-depth"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="fast">Fast</SelectItem>
                    <SelectItem value="balanced">Balanced</SelectItem>
                    <SelectItem value="deep">Deep</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={postComments} onChange={(event) => setPostComments(event.target.checked)} disabled={busy} />
                Publish summary and inline findings to the request
              </label>
              <p className="text-sm text-muted-foreground">Examines relevant source at the request’s head commit. Coverage and unverified requirements appear in the results.</p>
            </> : <p className="text-sm text-muted-foreground">The title must start with “Notif MR” (any letter case). Matching requests receive a posted summary and inline findings; other requests are skipped.</p>}
            <Button type="submit" disabled={busy || modelsLoading || !url.trim() || (mode === "TASK_AWARE" && taskPrompt.trim().length < 3)}>
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
            {job?.status === "reviewing" && job.phase && <p className="text-muted-foreground">{job.phase.replaceAll("_", " ")}</p>}
            {job?.status === "failed" && <p className="text-muted-foreground">Check the request for comments before retrying.</p>}
            {job?.result && <div className="space-y-4 border-t pt-4">
              <p className="break-all">Reviewed commit: <code>{job.result.reviewed_commit_sha || "Unavailable"}</code></p>
              <p>{job.result.partial ? "Partial verification" : "Verification completed"} · Publishing: {job.publishing_status}</p>
              <div className="space-y-3">
                {job.result.requirements.map((requirement) => <div key={requirement.id} className="rounded-md border p-3">
                  <p className="font-medium">{requirement.id}: {requirement.description}</p>
                  <p>{requirement.status.replaceAll("_", " ")}</p>
                  <p className="text-muted-foreground">{requirement.uncertainty}</p>
                  {requirement.evidence.map((evidence, index) => <details key={index} className="mt-2">
                    <summary>{evidence.path}:{evidence.start_line}–{evidence.end_line}</summary>
                    <pre className="overflow-auto whitespace-pre-wrap p-2 text-xs">{evidence.quote}</pre>
                  </details>)}
                </div>)}
              </div>
              {job.result.findings?.map((finding, index) => <div key={index} className="rounded-md border p-3">
                <p className="font-medium">{finding.severity}: {finding.title}</p>
                <p className="break-all text-muted-foreground">{finding.path}:{finding.line} · {finding.location}</p>
                <p className="whitespace-pre-wrap">{finding.body}</p>
              </div>)}
              <details><summary>Retrieved source files ({job.result.coverage.retrieved_files.length})</summary>
                <ul className="list-inside list-disc break-all">{job.result.coverage.retrieved_files.map((path) => <li key={path}>{path}</li>)}</ul>
              </details>
              {job.result.limitations.map((limitation, index) => <p key={index} className="text-muted-foreground">{limitation}</p>)}
            </div>}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

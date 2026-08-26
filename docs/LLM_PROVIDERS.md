# LLM providers

DiscoveryGram talks to nine providers. Six of them speak the same dialect and share one adapter;
three need their own. This page is the per-provider setup and the quirks that are not obvious from
`.env.example`.

For the variable reference see [CONFIGURATION.md](CONFIGURATION.md#llm-router); for the routing
design — the ladder, the breaker, the retry rules — see
[ARCHITECTURE.md](ARCHITECTURE.md#6-llm-router).

---

## The rule that applies to all nine

A provider needs **three** things to appear in a ladder, and missing any one of them removes it
with a reason in the startup log rather than a failure at request time:

1. it is named in `LLM_CHAIN_CHAT` or `LLM_CHAIN_VISION`;
2. it has an API key (`ollama` is exempt — it needs none);
3. it has at least one model listed for that task — `<P>_MODELS` for chat, `<P>_VISION_MODELS`
   for vision.

Run `make check-env` to print the exact ladder your configuration produces, with a line for every
provider that was skipped and why. That is the fastest way to answer "why is it not using
X?" — it makes no network calls and needs no running bot.

---

## OpenAI-compatible providers

`nvidia`, `openrouter`, `groq`, `cerebras`, `mistral` and `ollama` all serve
`POST {base}/chat/completions` with OpenAI's request and response shapes, and share
`discoverygram.llm.base.OpenAiCompatibleClient`. They differ only in the default base URL, in
whether they can carry an image, and in which errors they favour.

| Provider | Default `<P>_BASE_URL` | Key | Images |
|---|---|---|---|
| `nvidia` | `https://integrate.api.nvidia.com/v1` | required | yes |
| `openrouter` | `https://openrouter.ai/api/v1` | required | yes |
| `groq` | `https://api.groq.com/openai/v1` | required | yes |
| `cerebras` | `https://api.cerebras.ai/v1` | required | **no** |
| `mistral` | `https://api.mistral.ai/v1` | required | yes |
| `ollama` | `http://localhost:11434/v1` | none | yes |

**Quirks worth knowing**

- **`cerebras` is text-only.** It is dropped from the vision ladder at startup even if you set
  `CEREBRAS_VISION_MODELS`, and the reason is logged. Putting it in `LLM_CHAIN_VISION` is not an
  error — it simply contributes nothing there.
- **`ollama` needs no key and no `Authorization` header.** An empty bearer token is worse than
  none: some proxies reject it outright, so the header is omitted rather than blanked.
- **`ollama`'s URL is corrected for you.** Its native API is at the root and its OpenAI-compatible
  surface at `/v1`; a bare `http://host:11434` gets the suffix added rather than 404-ing on every
  call. Inside Docker, remember that `localhost` means *the container* — use
  `http://host.docker.internal:11434` or the host's LAN address.
- **`openrouter` is sent attribution headers** (`HTTP-Referer`, `X-Title`). They are not secrets;
  unattributed traffic is rate-limited harder.
- **Text-only messages keep the plain-string `content` form.** The OpenAI "parts" array is used
  only when an image is genuinely present, because not every OpenAI-compatible server implements
  it.

**An unlisted provider.** Setting `<P>_BASE_URL` for a name DiscoveryGram does not know makes it an
OpenAI-compatible provider — the escape hatch for a gateway or proxy. Without a base URL, an
unknown name is refused with the variable named.

---

## Gemini

Google's Generative Language API is not OpenAI-compatible in three ways that matter:

- the key rides in the **`x-goog-api-key` header**, never in a `?key=` query string — a query
  string ends up in access logs and proxy traces, and this one is a secret;
- the **model is part of the URL path**, so every model is a different endpoint;
- there is **no `system` role**. A system message is lifted out of the conversation and sent as
  `systemInstruction`; roles become `user` / `model`.

Images are sent as `inline_data` with raw base64 — no `data:` URL prefix.

```
GEMINI_API_KEY=AIza...
GEMINI_MODELS=gemini-2.0-flash
GEMINI_VISION_MODELS=gemini-2.0-flash
# GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
```

A model name may be written with or without the `models/` prefix; both work.

**Safety blocks are treated as model-level failures, not transient ones.** A `SAFETY`,
`RECITATION` or `PROHIBITED_CONTENT` finish reason means retrying the same rung gets the same
answer, so the router advances to the next model instead of burning its retries.

---

## Cloudflare Workers AI

Two things separate it from the rest.

**The account id is in the URL.** `CLOUDFLARE_API_KEY` alone is not enough:

```
CLOUDFLARE_API_KEY=...
CLOUDFLARE_ACCOUNT_ID=...           # required, or cloudflare is skipped at startup
CLOUDFLARE_MODELS=@cf/meta/llama-3.1-8b-instruct
CLOUDFLARE_VISION_MODELS=@cf/llava-hf/llava-1.5-7b-hf
```

A `cloudflare` in a chain without an account id is dropped when clients are built, with
`CLOUDFLARE_ACCOUNT_ID` named in the log and in the skip reason `/status` reports. It is never a
404 per attempt.

**A `200` can be a failure.** Workers AI reports application errors as `success: false` with an
`errors` array, so the HTTP status alone never decides whether a call worked. Error codes 1000,
9106 and 10000 are read as credential failures and open the provider's circuit immediately.

Images travel in an `image` field alongside the text, not inside it — the OpenAI parts form is not
accepted.

---

## Puter

Puter has no chat-completions endpoint at all. Everything goes through one RPC call:

```
POST {base}/drivers/call
{"interface": "puter-chat-completion", "method": "complete", "args": {...}}
```

The answer is nested under `result`, and — as with Cloudflare — an application failure arrives as
`200` with `success: false`.

```
PUTER_API_KEY=...
PUTER_MODELS=gpt-4o-mini
PUTER_VISION_MODELS=gpt-4o-mini
# PUTER_BASE_URL=https://api.puter.com
```

**Put it last in a chain.** Its API carries no version guarantee and it fronts several back ends
whose response shapes differ — the adapter reads the answer from `result.message.content` (string
*or* parts) and from a bare `result.text`, and accepts both `prompt_tokens` and `input_tokens`.
`PUTER_BASE_URL` exists so the endpoint can be corrected without a release.

`insufficient_funds` is mapped to a rate limit rather than an auth failure: the provider is fine,
this *request* cannot be paid for, and the next model may be cheaper.

---

## Choosing a chain

A chain is only failover if it names **more than one provider**. Extra models of a single provider
buy you retries against different weights; they do not survive that provider being down, and
`make check-env` warns when a ladder has this shape.

A reasonable starting point:

```
LLM_CHAIN_CHAT=groq,cerebras,openrouter,ollama
LLM_CHAIN_VISION=gemini,openrouter,ollama
```

- Fast hosted providers first, because most requests succeed on the first rung.
- A different *company* second, so a regional outage does not take the whole chain.
- **`ollama` last as a terminator**: local, keyless, and unable to run up a bill. A chain that ends
  locally degrades to "slower" rather than to "broken" when every hosted provider is failing.

---

## Reading the failures

| Symptom | What it means | Where to look |
|---|---|---|
| A provider vanished from the ladder | No key, no model for the task, or an unbuildable client | `make check-env`, or the `llm_ladder_built` log line |
| `/status` shows `circuit open` | That provider failed enough to be short-circuited; it is skipped until the cool-down ends | `LLM_CIRCUIT_RESET_S`, and the `llm_circuit_opened` log line naming the reason |
| One request, many log lines | Normal: every *attempt* is logged, one request may walk several rungs | `llm_attempt` records; `llm_request_served` names the rung that won |
| "Every configured … model failed (N tried)" | The whole ladder was exhausted. The message carries the last rung's own error | `llm_ladder_exhausted` |
| A user is told they are out of AI requests | `LLM_DAILY_CALL_LIMIT_PER_USER`, counted per UTC day. Failover does **not** cost extra: one request is one call | Set it to `0` to disable the cap |

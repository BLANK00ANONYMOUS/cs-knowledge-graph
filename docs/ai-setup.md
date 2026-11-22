# Setting up the AI tutor

The app's AI features need **one AI provider that you bring yourself**. Nobody shares an account or a key: each person
sets this up on their own machine, and no key is ever stored in the repository or in `kg.config.yaml`.

## Do I need it?

| Works without any AI | Needs the AI |
|---|---|
| `kg check`, `next`, `review`, `rate`, `search`, `ingest`, `progress`, `new`, `build` | 📘 Lessons, 🎓 quizzes, study plans and the tutor chat |
| `kg judge` (testing your solution to a challenge) | ➕ Adding a topic (planning the concept and its prerequisites) |
| The static graph from `kg build` (`build/graph.html`) | 🏁 Generating challenges, hints and code reviews |
| | 💼 Analysing a job role or posting, suggesting roles |
| | 🛠 Tagging synced GitHub work with concepts, finding issues, project ideas |

> **Note:** `kg serve` (the web app) currently won't start at all if the chosen provider isn't available, even though
> some of its pages don't use AI. It exits with a message saying what's missing.

## Pick a provider

| | Provider | You need | Cost | Cites your books |
|---|---|---|---|---|
| 1 | `claude-code` (default) | A Claude account (Pro or Max) and Claude Code or the Claude desktop app | Your Claude plan, no API key | No |
| 2 | `copilot` | A GitHub Copilot subscription and the GitHub CLI | Your Copilot plan | Yes |
| 3 | `anthropic` | A Claude API key | Pay per use | Yes |
| 4 | `openai` (cloud) | An OpenAI or Gemini API key | Pay per use (Gemini has a free tier) | Yes |
| 5 | `openai` (local) | Ollama or LM Studio and a downloaded model | **Free**: no account, no key, works offline | Depends on the model |

**Which one?**
- **Already pay for Claude:** use option 1.
- **Want the best book citations:** use option 2 or 3.
- **Want it free and private:** use option 5, but expect weaker lessons and quizzes from a small local model.

**Book citations** come from the tutor searching your PDFs while it writes. The `claude-code` provider can't call that
search, so it teaches from general knowledge and leaves citations empty instead of inventing page numbers.

All settings go under `tutor:` in `kg.config.yaml`. You can also switch for one run without editing the file:

```bash
./kg serve --provider anthropic --model claude-sonnet-5-5      # Windows: .\kg.cmd serve --provider …
```

`--provider` on its own ignores `tutor.model` (that model belongs to the configured provider) and uses the new
provider's default.

---

## 1. Claude subscription (`claude-code`, the default)

No API key: the app runs the Claude Code command-line tool, signed in to your own Claude account.

1. **Get Claude Code.** Either install it on its own, or install the Claude desktop app, which includes it. The app
   finds it automatically on your `PATH`, at `~/.claude/local/claude`, or inside the Claude desktop app (macOS and
   Windows).
2. **Sign in once:**
   ```bash
   claude auth login
   ```
   If `claude` isn't on your `PATH`, start `./kg serve`: the error message prints the full path of the copy the app found,
   so you can run `"<that path>" auth login`.
3. **Configure** (this is the default already):
   ```yaml
   tutor:
     provider: claude-code
     model: sonnet          # sonnet | opus | haiku, or a full model id
     # claude_path: /path/to/claude   # only if it isn't found automatically
   ```

The app removes `ANTHROPIC_API_KEY` and similar variables before it starts Claude Code, so this option always uses your
subscription and never quietly bills an API key.

## 2. GitHub Copilot (`copilot`)

1. Install the Copilot SDK. It isn't installed by default:
   ```bash
   ./setup.sh --with-copilot          # Windows: .\setup.ps1 -WithCopilot
   ```
2. Sign in with a GitHub account that has Copilot:
   ```bash
   gh auth login
   ```
   Alternatively, set `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or `GITHUB_TOKEN`.
3. Configure:
   ```yaml
   tutor:
     provider: copilot
     model: claude-sonnet-5.5   # any model your Copilot plan offers
   ```
   If that model isn't available on your plan, the app falls back to another one automatically and says so in the
   terminal.

## 3. Claude API (`anthropic`)

1. Create an API key in the Anthropic Console.
2. Put it in an environment variable, never in a file in this repository:
   ```bash
   export ANTHROPIC_API_KEY=...        # macOS/Linux: add it to ~/.zshrc or ~/.bashrc to keep it
   ```
   ```powershell
   setx ANTHROPIC_API_KEY "..."         # Windows: then open a new terminal
   ```
3. Configure:
   ```yaml
   tutor:
     provider: anthropic
     model: claude-sonnet-5-5
     # max_tokens: 16000          # raise it if you see "the model's answer was cut off"
     # api_key_env: MY_KEY_VAR    # if your key lives in a differently named variable
   ```

## 4. OpenAI or Gemini (`openai`)

Any service that speaks the OpenAI API works.

```bash
export OPENAI_API_KEY=...               # Windows: setx OPENAI_API_KEY "..."
```

```yaml
tutor:
  provider: openai
  model: gpt-5                           # the service's model id (required for this provider)
  # base_url: https://api.openai.com/v1  # the default
```

For **Google Gemini**, put your Gemini key in `OPENAI_API_KEY` (or name another variable with `api_key_env`) and set:

```yaml
tutor:
  provider: openai
  model: gemini-2.5-flash                # e.g.; use a current Gemini model id
  base_url: https://generativelanguage.googleapis.com/v1beta/openai/
```

## 5. A free local model (`openai` with Ollama or LM Studio)

Everything runs on your computer: no account, no key, nothing leaves your machine.

**Ollama**

1. Install Ollama from ollama.com, then download a model:
   ```bash
   ollama pull llama3.1
   ```
2. Configure (no key needed when `base_url` is set):
   ```yaml
   tutor:
     provider: openai
     model: llama3.1
     base_url: http://localhost:11434/v1
   ```
3. **Give it room.** Ollama's default context window is small, and long lessons get cut off. If you see "the model's
   answer was cut off", raise `num_ctx` for the model in Ollama.

**LM Studio:** load a model, start its local server, and use `base_url: http://localhost:1234/v1` with the model name
LM Studio shows.

**Pick a capable model.** The tutor asks for structured JSON and, for book citations, tool calls. Small models (around
3B parameters) often fail at both. Models that support tool calling, around 8B parameters and up, work much better.

---

## Check that it works

```bash
./kg serve --open          # Windows: .\kg.cmd serve --open
```

The terminal shows the provider and model it uses, for example `(Claude (subscription), model: sonnet)`, and the Graph &
Tutor page shows them above the chat. Open a concept and press **📘 Learn**: a lesson should arrive in under a minute.

## Optional settings

```yaml
tutor:
  verify_answers: true   # a second, independent AI call checks every quiz answer key (one extra call per question)
  questions: 6           # questions per quiz
```

## Troubleshooting

| Message | What to do |
|---|---|
| `couldn't find the Claude Code CLI` | Install Claude Code or the Claude desktop app, or set `tutor.claude_path`. |
| `Claude Code isn't signed in` | Run the `auth login` command the message shows, then try again. |
| `the anthropic provider needs an API key: set ANTHROPIC_API_KEY` | Set the variable in the terminal that starts `kg serve`, or open a new terminal after `setx`. |
| `the openai provider needs an API key` | Set `OPENAI_API_KEY`, or set `base_url` for a local server (no key needed). |
| `set tutor.model in kg.config.yaml for the openai provider` | This provider has no default model: name one. |
| `needs the SDK: pip install …` | Run setup again, or `.venv/bin/pip install anthropic` / `openai`. |
| `GitHub Copilot didn't start within a minute` / `No GitHub OAuth token` | Run `gh auth login` with an account that has Copilot, then restart. |
| `no Copilot model is available right now` | Check your Copilot plan, or set a different `tutor.model`. |
| `the model's answer was cut off` | Retry; raise `max_tokens` (Claude API) or `num_ctx` (Ollama), or use a larger model. |
| `the model returned malformed output` | Retry. If it keeps happening with a local model, use a larger one. |

## Privacy

- **What goes to your provider:** the relevant concept notes, your answers, and (for providers that can search) short
  snippets from your books.
- **Local option:** with option 5 nothing leaves your computer.
- **Where keys live:** in your environment or your provider's own sign-in. The app never writes them to the repository.

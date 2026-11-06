# Dupaco Knowledge Solution

## Approach

### 1. Storage

The system uses a single SQLite database to store:

- Versioned website pages and snapshot metadata.
- Passages used for retrieval.
- BM25 postings and document-frequency tables.
- Parsed rate tables and their indexes.
- Answer cache and conflict-detection data.

`kbcore.py` defines the database schema.

The `KB_DB=/path/file.sqlite` environment variable allows the database to persist between runs. Reopening an existing database does not require re-ingestion, and ingesting an already processed snapshot is a no-op.

By default, a temporary database is created and deleted on exit.

Each thread uses its own SQLite connection with WAL mode to support concurrent scorer requests.

### 2. Versioned Page Store

Every `(URL, content hash)` version records the bitmask of snapshots in which it appeared.

Pages that disappear from a later snapshot remain available as their last recorded version.

The `as_of` parameter selects the newest snapshot taken on or before the requested date. For dates before the first snapshot, the system returns an answer indicating that the information cannot be determined.

### 3. Retrieval

The system uses BM25 retrieval over passages, implemented through SQLite postings tables using the same scoring formula as the original in-memory version.

Retrieval also considers:

- Title and URL bonuses.
- Entity boosts.
- Page-kind authority multipliers: official rates/product pages rank above promotional pages and files, followed by news/blog pages and calculators.
- Per-snapshot validity filtering using the page-version bitmask.

### 4. Structured Rate Tables

Rate tables are parsed into structured rows.

The question is matched against relevant attributes, including:

- Product and account terms.
- Model-year ranges.
- Dollar tiers.
- Product qualifiers such as jumbo, Roth, refinance, and business.
- Requested columns such as APY, dividend rate, and APR.

The system handles superlative questions, questions asking whether any tiers pay a specified rate, and list questions.

When required product qualifiers are missing, the system refuses to guess.

### 5. Specialized Handlers

Dedicated handlers support:

- Branch hours.
- Application and relevant link discovery.
- Count questions, such as the number of branches.
- Date-bearing statements, including closing and effective dates.

### 6. Refusals

Unknown entities and unsupported products return `confident=False` without inventing numerical values.

### 7. Optional LLM Step

The optional LLM integration is implemented in `llm.py`.

For free-text questions, the system sends the top retrieved evidence excerpts to OpenAI GPT, which generates a concise answer grounded in that evidence.

- Numerical claims must appear in the retrieved evidence or the question.
- If the generated answer does not satisfy this constraint, the system falls back to the rule-based answer.
- Structured rate answers are handled deterministically and do not go through the LLM.

### 8. Conflict Detection

Every rate-table row from every page version is treated as a fact keyed by product, tier/term/year, and column.

These facts are written to temporary conflict-detection tables in the same SQLite database (`cf_*`), which are dropped after use.

When values differ, the system identifies a conflict. The winning value is determined using the following order:

1. Presence in the newest snapshot.
2. Page authority.
3. Effective date.
4. Majority agreement.

The system also detects Roth-versus-Traditional mismatches and repeated prose counts, such as branch and employee counts.

## How "Current" Is Decided

The system determines the current value using the following priority:

1. Latest snapshot, or the snapshot selected by `as_of`.
2. Official page kind.
3. Effective date printed on the table.
4. Document date from the URL or byline.
5. Newest snapshot.

Undated website pages are treated as having the date of their snapshot.

## Environment Setup

The project is intended to run in a clean Python 3.11 environment.

### 1. Create and Activate a Virtual Environment

**macOS/Linux:**

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

**Windows (PowerShell):**

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell blocks script execution, activate the environment using:

```powershell
.venv\Scripts\activate.bat
```

### 2. Install Dependencies

On macOS/Linux and Windows:

```bash
pip install -r requirements.txt
```

### 3. Configure the OpenAI API Key

The project supports optional GPT-based free-text answer generation using the OpenAI API.

**macOS/Linux:**

```bash
export OPENAI_API_KEY="your-openai-api-key"
export KB_PROVIDER="openai"
export KB_MODEL="gpt-4o-mini"
```

**Windows (PowerShell):**

```powershell
$env:OPENAI_API_KEY="your-openai-api-key"
$env:KB_PROVIDER="openai"
$env:KB_MODEL="gpt-4o-mini"
```

Replace `your-openai-api-key` with a valid API key obtained from OpenAI.

Do not commit API keys or `.env` files to the repository.

### 4. Run the Evaluation

From the repository root, with the virtual environment activated:

**macOS/Linux and Windows:**

```bash
python3 harness/run_eval.py --submission solution --eval eval/public_eval.jsonl
```

## Keys and Paid API Usage

The system works without an API key using its deterministic, rule-based retrieval and answer-generation path.

OpenAI GPT is used optionally for free-text answer generation.

| Environment variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | OpenAI API key |
| `KB_PROVIDER=openai` | Explicitly selects OpenAI |
| `KB_MODEL` | Overrides the default model |
| `KB_USE_LLM=0` | Disables LLM usage |
| `KB_TIMEOUT` | Request timeout; default is 14 seconds |

The default OpenAI model is `gpt-4o-mini`.

Anthropic is also supported as an optional alternative provider through `ANTHROPIC_API_KEY` or `ANTHROPIC_AUTH_TOKEN`. It is not required for the OpenAI configuration.

OpenAI API usage may incur charges. Anyone running the GPT-enabled configuration must provide their own valid API key.

LLM calls are subject to the 60-second answer limit. Ingestion does not make external API calls.

# Intent classifier: options without an LLM

How to answer "did the caller agree?" (and similar small questions) with a
fast, cheap, reusable classifier instead of a model request.

## 1. The problem

Today every consent check is one Gemini request
(`salon/classification.py` → `core/classifiers.py`). It fills four labels in
`ReplyClassification`:

| Label | Question it answers | Difficulty without an LLM |
|---|---|---|
| `confirmation` | Did they accept, reject or change the thing we asked? | Easy |
| `conversation` | Do they want to end the call? | Medium ("no thanks, that's all" vs "no" to an offer) |
| `appointment_choice` | Move the old booking, or add a new one? | Medium ("instead", "replace" vs "also", "another") |
| `retained_approval` | Did they change topic after approving a complaint? | Hard, keep on the LLM for now |

**Why a simple classifier is enough.** The app already knows the exact
question it asked (`Asked.about`: phone, booking, move_or_add, complaint,
closing) and the exact value (the number or slot read back). The classifier
never has to understand the whole call. It only answers: "is this short reply
yes, no, a change, or unclear?" That is a small, closed problem.

## 2. Requirements

- **Reusable** across use cases, not only the salon.
- **Milliseconds** per answer.
- **CPU only**, scales with no GPU.
- **Low memory.**
- **Multilingual.**
- **Safe:** when unsure, say `unclear` so the app asks again. A wrong
  "unclear" costs one repeated question; a wrong "accept" costs a wrong booking.

## 3. Options reviewed

| Approach | Memory | Speed per call | New dependency | Verdict |
|---|---|---|---|---|
| Keyword rules (plain Python) | ~0 | microseconds | none | **Keep**, first layer |
| Character n-gram matching (plain Python) | KB per spec | < 1 ms | none | **Keep**, handles typos and STT noise |
| model2vec static embeddings | tens of MB (English), 100+ MB (multilingual)* | ~1 ms | `model2vec` (numpy only) | **Keep as upgrade**, only if needed |
| spaCy | 10–500 MB per language | 1–5 ms | yes | **Drop**: gives tools to write rules, does not decide yes/no |
| NLTK (incl. VADER sentiment) | data downloads | fast | yes | **Drop**: sentiment is English only and the wrong question |
| fastText | often hundreds of MB | fast | yes, unmaintained | **Drop** |
| sentence-transformers / SetFit | PyTorch: hundreds of MB to GB | 5–20 ms | yes, heavy | **Drop** |
| Zero-shot NLI (e.g. mDeBERTa-xnli) | same as above | 50–200 ms | yes, heavy | **Drop** |

\* Sizes from memory; measure before deciding.

## 4. Why not sentiment (positive / negative)

Sentiment says whether someone *sounds* happy. Consent is whether they *agreed*.
On calls these often disagree:

| Caller says | Sentiment | Real meaning |
|---|---|---|
| "No, no, that's perfect" | negative words | **accept** |
| "Great! But can we do 5 instead?" | positive | **revise** |
| "Sorry, yes, that's right" | negative-ish | **accept** |
| "I'd love to, but I can't" | positive | **reject** |
| "No problem" | negative words | **accept** |

So the labels stay `accept / reject / revise / conditional / unclear`.

NLTK's multilingual parts (tokenizers, stopwords, Snowball stemmers) only tidy
text; plain Python can do the tidy-up we need.

**Bag of words, however, is the right idea**: "which words or word pieces are
in the reply" is exactly the signal for short answers.

## 5. Making it multilingual

### Option A: shared logic + word lists per language (recommended base)

Logic written once, works in every language:

- The last clause wins: "yes... no, wait" → no.
- A yes plus "but" or a new time/date → revise.
- A yes word inside a negation ("not bad", "no problem") → yes.
- No match, or yes and no with no clear winner → unclear.

Word lists per language, kept as data, not code:

```
confirm/en: accept = yes, yeah, yep, correct, right, sure, perfect ...
            reject = no, nope, wrong, not right ...
            but    = but, however, instead ...
confirm/es: accept = sí, vale, correcto, perfecto, claro, eso es ...
            reject = no, incorrecto, para nada ...
            but    = pero, sin embargo, mejor ...
confirm/it: accept = sì, certo, esatto, va bene, perfetto ...
```

- **No language detection needed**: `bot.py` already sets the call language on
  the STT service (`Language('en')`), so the classifier loads that list.
- New language = one small file of 30–50 words, reviewable by a native speaker
  in minutes.
- Memory ~0, speed in microseconds.

### Option B: character n-grams trained on examples from all languages

Compare small letter chunks ("sí p", "perf", "yeah") with example replies in
every supported language. One model for all languages; copes with typos and
STT errors ("yeh", "si" without accent). Plain Python, KB of memory. Only knows
languages you gave examples for.

### Option C: multilingual static embeddings (model2vec)

The only option where English examples also match Spanish or German replies you
never wrote, because the model already knows "yes", "sí" and "ja" mean the same.
numpy only, ~1 ms, but the multilingual model is much bigger (likely 100+ MB per
process). Use only if A and B are not enough.

## 6. Recommended design

### Generic, data-driven specs

A classifier is configured by a **spec**, not code. The engine is shared.

```python
CONFIRM = ClassifierSpec(
    labels={
        'accept': ['yes', 'yeah that is right', 'correct', 'sí', 'vale', 'perfecto'],
        'reject': ['no', 'that is wrong', 'nope', 'no es correcto'],
        'revise': ['yes but at five', 'can we do tuesday instead'],
    },
    rules=[...],
    threshold=0.6,
    fallback='unclear',
)
```

- **One small classifier per kind of question** ("confirm", "pick between two",
  "end the call"), not one that fills four labels. Most use cases reuse
  "confirm" and "end call" as they are.
- The app picks the spec from the question it asked (`Asked.about`).
- Engine lives in `core/` (no salon imports); specs live with each use case.
- Same `classify(input) -> output` shape as `core/classifiers.py`, so
  `classify_reply` and the tools do not change.
- If a model is used, it is loaded **once per process** and shared by all specs;
  each spec adds only a few KB.

### Pipeline per reply

1. **Clean**: lowercase, strip accents and punctuation, fold "yeah/yep/ya".
2. **Rules**: per-language word lists + shared logic. Clear cases stop here.
3. **Nearest example**: character n-gram similarity to the spec's examples.
4. **Safety check**: low score, or top two labels too close → `unclear`.
5. **Optional LLM fallback**: off by default, per use case, only for `unclear`.
6. **Later, if needed**: swap step 3 for model2vec.

Steps 1–4: no new dependency, a few hundred KB, under 1 ms on CPU, no network,
stateless and safe to share across calls.

## 7. Limits

- Short, direct answers: very good. Long mixed replies ("well, I guess, but
  only if my sister can come too") will often be `unclear`. Safe, but the
  caller hears the question again more often than with an LLM.
- Word lists and n-grams only know languages you supply.
- `retained_approval` is not a yes/no problem; keep it on the LLM for now.
- Adds language-specific data, like the existing `times_named` and complaint
  word list debt noted in `CLAUDE.md`.
- `CLAUDE.md` asks before any new dependency (model2vec would be one).

## 8. Before switching

Build a labelled set of ~200 real replies from call logs. Run the current LLM
and the new classifier on it; compare accuracy and latency. No accuracy claims
until then.

## 9. Suggested first step

Generic engine in `core/` with rules + character n-grams (plain Python), English
and Spanish word lists for "confirm", wired to salon `confirmation` with the LLM
kept as fallback, plus a test module.

# Support that remembers and checks

A hands-on demonstration of an AI support assistant investigating missing savings,
remembering earlier conversations, and answering product questions with evidence.
Built for a Moniepoint engineering walkthrough using Redis and Iris agent memory.

**This is a fictional demonstration, not Moniepoint's live support system.**
The customers, payments, and account changes are simulated. No real money moves.
The product guidance is based on summaries of public Moniepoint help articles;
the support workflow and service APIs are examples built for this project.

## The story

A customer says: **“My transfer completed, but I can't see any savings.”**

A useful assistant needs more than a plausible answer. It must find the payment,
check the savings settings that applied at the time, see what processing did,
and check whether savings were actually recorded.

If a settings change would help, the assistant proposes it. The customer sees
an approval card and chooses whether to proceed. The application checks the
proposal before applying it. An AI response or a remembered claim of consent
cannot approve a change.

When the customer returns later, memory can help the assistant recognise the
earlier issue. It must still check what is true now.

## What you can explore

| Feature | What it means in everyday language |
| --- | --- |
| Short-term memory | Include earlier messages from the current conversation when preparing an answer. |
| Long-term memory | Recall useful observations from an earlier conversation with the same customer. |
| Semantic cache | Reuse a previously checked policy answer when it also answers the new question. The wording need not be identical. |
| Behind the answer | See which services were checked, what they returned, and how long the answer took. |
| Approval cards | Let the customer approve or decline a specific proposed settings change. |
| Policy updates | Update a guidance document and prevent old cached answers from being reused against its previous version. |

The switches are inside **Demo controls**. They apply when you click **New session**.
Changing a switch does not change an existing conversation.

**New session starts a new conversation for the same customer.** Account settings,
existing long-term memories, and shared policy answers remain stored. Turning
long-term memory off stops recall and recording for that session; it does not
delete previously stored memories.

## A five-minute tour

Once the app is running, open [Support studio](http://127.0.0.1:5173/).
Choose a fictional customer, open **Demo controls**, and start a new session.
You type the messages yourself; the assistant's replies and tool choices are not scripted.

### 1. See the difference between visible chat and memory

Turn all three switches **off**. Send these as two separate messages:

> For this demonstration, my reference word is BLUEBIRD. Please acknowledge it.

> What reference word did I just give you?

The earlier message stays visible on screen, but the model does not receive it
with the second request. It should be unable to identify the word from that request.

Now turn **Short-term memory on**, leave the other switches off, and click
**New session**. Repeat both messages. The assistant can now use the first message.
This is the application supplying history, rather than the model remembering by itself.

### 2. Investigate missing savings

Choose **Emeka**, enable short-term memory, and start a new session:

> My ₦12,000 transfer completed, but I cannot see any savings from it. Can you investigate?

Open **Behind the answer** to follow the checks. In Emeka's initial account,
card payments trigger savings but transfers do not. If you choose to enable
transfers, ask the assistant to propose the change and use the approval card.
The change affects future eligible payments; it does not repair earlier ones.

The account may differ if someone has already used it. The assistant should
check live records instead of assuming these starting settings still apply.

### 3. Return to an earlier issue

For this part, long-term memory must be configured (see setup below). Enable
both memory switches before starting the first conversation. Investigate a
payment, then inspect the recorded events in **Behind the answer**.

Wait for the memory service to extract observations. Recording an event and
extracting a memory are separate steps; extraction is not immediate. Use
**Check extracted observations** to see whether relevant observations are available.
Then start a new session for the same customer and ask:

> Can we return to the missing savings we discussed earlier?

The point to inspect is whether the assistant recalls relevant context and then
checks current facts. A fresh installation has no preloaded conversation memories.
A new session with long-term memory off is your comparison case.

### 4. Compare cached and freshly generated answers

Turn both memory switches off. With **Semantic cache off**, start a session and ask:

> What is Save As You Transact and how does it work?

Enable semantic cache, start another session, and ask again. The first eligible
answer is checked and stored in the background. After storage completes, ask
again in another cache-enabled session, or try a rewording:

> Can you explain how Save As You Transact works?

In **Behind the answer**, inspect the policy-question tool. A successful reuse
shows `cache_hit` and `answer_source: cache`. A close match is not automatically
accepted: the verifier may reject it and generate a new answer instead.

The displayed answer time includes the wider support-agent flow. A faster policy
lookup does not remove the time the support agent takes to write its final reply.

## Meet the fictional customers

These are starting fixtures. Each customer has their own payments, savings plan,
and conversations.

| Customer | Starting savings settings | Example to investigate |
| --- | --- | --- |
| Ada | ₦500 per card payment | An excluded transfer; an earlier plan versus a later change. |
| Chidi | ₦250 per card payment or transfer | A transfer whose processing event arrived late. |
| Bisi | ₦100 per airtime purchase or bill payment | A transfer outside the selected categories. |
| Emeka | ₦500 per card payment | A completed ₦12,000 transfer without savings. |
| Zainab | ₦250 per card payment or transfer | A delayed transfer despite transfers being enabled. |

All five start without conversation memories on a new installation. Sessions and
settings persist when you restart the app. Resetting the simulated account is not
the same as deleting its memories from Iris.

## What memory can and cannot tell you

Memory can preserve an observation such as “the customer previously asked about
missing savings on a transfer.” It is a clue for the next investigation.

It is not the authoritative record of a balance, payment status, current savings
plan, or permission to make a change. Those come from services and explicit
customer decisions. The demo distinguishes customer reports, verified findings,
and action outcomes when recording observations.

The application masks some common sensitive patterns before sending events to
Iris. This is a simple demonstration filter, not complete personal-data protection.
Original chat text is still stored in local conversation checkpoints and is sent
to the model provider. Use fictional information only.

## Run it on your computer

This section is for the person setting up the demo. Everyone else can use the
browser once it is running. There is no hosted public demo included.

### What you need

- **Python 3.12 or later** and **uv**, which installs the Python dependencies.
- **Node.js 22 or later** and npm, which run the browser interface.
- **Docker with Compose**, which runs a local Redis database.
- An **OpenAI API key** for the assistant, policy answers, and search embeddings.
- An **OpenRouter API key** with access to the configured **Jev** verifier for the
  semantic-cache demonstration.
- Optionally, a **Redis Iris agent-memory store** and its endpoint and API key.
  Long-term memory requires the `support_observation` custom type. See the
  [technical guide](docs/technical-guide.md#iris-memory) for its intended contents.

External model and memory services may incur charges. Model names are configurable
in `.env`; your account needs access to the selected models.

### Start the services

Download this repository with GitHub's **Code → Download ZIP**, extract it, and
open a terminal in the extracted folder. Or, if you use Git:

```sh
git clone https://github.com/s-agbede/moniepoint-iris-demo.git
cd moniepoint-iris-demo
```

Create your local configuration file:

```sh
cp .env.example .env
```

Open `.env` in a text editor and fill in the service credentials you will use.
Keep this file local; it is excluded from Git. The example file contains no keys.
For a basic memory-off chat, start with `OPENAI_API_KEY`. Configure the remaining
services before demonstrating their features.

Start the database, install dependencies, and start the backend:

```sh
docker compose up -d --wait
uv sync
uv run --env-file .env uvicorn moniepoint_demo.api:app --host 127.0.0.1 --port 8017
```

Leave that terminal running. In a second terminal, from the project folder:

```sh
cd frontend
npm ci
npm run dev
```

Open **http://127.0.0.1:5173/**. The optional developer API explorer is at
**http://127.0.0.1:8017/docs**.

To stop, press **Ctrl+C** in the two service terminals, then run
`docker compose stop` from the project folder. Redis data stays in `.local/redis/`
so it is available next time.

### If something does not work

| What you see | What to check |
| --- | --- |
| The app cannot load customers | Confirm the backend is running on port 8017 and Docker Redis is healthy. |
| The assistant cannot answer | Check the OpenAI key and configured model access, then restart the backend. |
| Long-term memory is unavailable | Configure all three `AGENT_MEMORY_…` values and the custom memory type, then restart the backend. |
| Events were recorded but no memories appear | Wait for extraction and check again; only relevant extracted observations appear in the search. |
| A reworded question does not hit the cache | The answer may not yet be stored, the question may differ, the source may have changed, or Jev may reject reuse. Read the trace. |
| A switch seems to do nothing | Set it first, then click **New session**. Existing sessions keep their settings. |
| Settings differ from this walkthrough | An earlier run may have changed the account. New session does not reset account data. |

## How it fits together

The browser sends the customer's message to the support application. The
application selects the permitted context, asks the model what to do, and runs
allowed tools. Tools inspect simulated services or retrieve product guidance.
The model uses those results to explain its answer.

- **React** provides the chat screen and approval cards.
- **FastAPI and Python** provide the application and simulated services.
- **LangGraph with Redis checkpoints** preserves the current conversation and
  supports resuming an interrupted turn.
- **Iris agent memory** extracts and recalls observations across conversations.
- **Redis search and a custom cache with Jev verification** support policy answers.
  This project does not use the managed LangCache product.
- **Redis Streams** carries policy-change events for cache cleanup. Source-version
  checks also prevent stale reuse before background cleanup finishes.

## Scope and limits

This is a local teaching application, not a production banking application.
It has a customer selector rather than login or production authentication.
Keep it bound to localhost. It does not implement real transfers, balances,
interest, fees, or full accounting. The simulated two-minute event freshness
rule is a demo assumption, not a claimed Moniepoint threshold.

The main comparison is **correct resolution with less repetition**. Timings vary
with the model, network, conversation, and external services. One successful run
is not a performance benchmark or a security guarantee. Memory can be missing,
stale, or wrong; the application must continue to check current facts.

## For developers

See the [technical guide](docs/technical-guide.md) for endpoints, source publication,
cache validation, approval handling, and persistence details. The source is under
`src/moniepoint_demo/`, the interface under `frontend/`, and automated tests under
`tests/`. Bundled guidance lives in `src/moniepoint_demo/knowledge/`.

With local Redis running on port 6387:

```sh
uv run pytest -q
uv run ruff check src tests
uv run mypy src tests
```

Build and type-check the interface:

```sh
cd frontend
npm ci
npm run build
```

Tests use separate Redis keys. The normal test suite uses controlled model
responses; it does not establish live provider availability or guarantee identical
answers from a real model.

## Background reading

- [Moniepoint: how Save As You Transact reacts to millions of events](https://engineering.moniepoint.com/save-as-you-transact-how-we-built-a-savings-product-that-reacts-to-millions-of-events)
- [Moniepoint: what is Save As You Transact?](https://support.moniepoint.com/topics/savings-198/what-is-save-as-you-transact-sayt-360)
- [Moniepoint: managing a SAYT plan](https://support.moniepoint.com/topics/savings-198/managing-your-sayt-plan-362)

The bundled guidance is a demonstration snapshot. Follow the linked official
sources for current product guidance.

# Comparison

How Highbar compares to existing AI testing and evaluation tools.

## Feature Comparison

| Feature | Highbar | Promptfoo | DeepEval | Braintrust | Manual Testing |
|---------|----------|-----------|----------|------------|----------------|
| Tests the agent (not just the LLM) | Yes | No | No | No | Sort of |
| Function call assertions | Native | Partial | No | No | Manual |
| Multi-turn testing | Native | No | No | No | Manual |
| Guardrail validation | Built-in | Partial | Partial | No | Manual |
| YAML spec files | Yes | Yes | No (Python) | No (Python) | N/A |
| No-code test authoring | Yes | Yes | No | No | N/A |
| Subset parameter matching | Yes | No | No | No | Manual |
| Template-based multi-turn data | Yes | No | No | No | Manual |
| Open source | MIT | MIT | Apache 2.0 | Proprietary | N/A |

## The Key Differentiator

Existing tools evaluate **the model** -- they test prompts, measure output quality, and benchmark LLM performance. Highbar tests **the agent** -- it verifies that the complete system (LLM + tools + orchestration logic) produces the correct behavior.

Consider an order support agent. Existing tools can tell you:

- "The model responds helpfully 92% of the time" (Promptfoo)
- "The response is factually accurate" (DeepEval)
- "The output quality score is 0.87" (Braintrust)

Highbar tells you:

- "The agent called `search_orders` with `order_id: '12345'`"
- "The agent did NOT call any tools when given a prompt injection"
- "The multi-turn refund flow completed with the correct function calls in sequence"

The LLM is an implementation detail. You could swap GPT-4 for Claude or a fine-tuned Llama -- the agent behavior tests remain the same.

## Detailed Comparison

### vs. Promptfoo

Promptfoo is an LLM evaluation framework focused on testing prompts and model outputs. It excels at comparing model responses across different prompts, providers, and configurations.

**Where Promptfoo fits:** Evaluating prompt templates, comparing model providers, measuring response quality metrics.

**Where Highbar fits:** Testing that an agent calls the right tools with the right arguments, validating multi-turn workflows, enforcing guardrails against prompt injection and PII leakage.

**Key differences:**
- Promptfoo sends prompts directly to LLM APIs. Highbar sends triggers to the agent's HTTP endpoint.
- Promptfoo evaluates text output. Highbar asserts on function calls AND text output.
- Promptfoo does not have native multi-turn support. Highbar threads data between turns using <code v-pre>{{previous.*}}</code> templates.

### vs. DeepEval

DeepEval is a Python testing framework for LLM applications, with built-in metrics like faithfulness, relevance, and hallucination detection.

**Where DeepEval fits:** Measuring output quality with statistical metrics, evaluating RAG pipelines, checking for hallucinations.

**Where Highbar fits:** Behavior testing -- did the agent do the right thing? Not just "was the output good?" but "did it call the right tool with the right arguments?"

**Key differences:**
- DeepEval uses Python test functions. Highbar uses YAML specs.
- DeepEval focuses on output metrics. Highbar focuses on behavior assertions.
- DeepEval does not capture or assert on tool calls.

### vs. Braintrust

Braintrust is a proprietary platform for evaluating and monitoring AI applications, with logging, scoring, and comparison features.

**Where Braintrust fits:** Production monitoring, A/B testing model configurations, tracking quality over time.

**Where Highbar fits:** Pre-deployment behavior testing in CI/CD pipelines.

**Key differences:**
- Braintrust is a hosted platform. Highbar is a local CLI tool.
- Braintrust focuses on observability and scoring. Highbar focuses on pass/fail behavior assertions.
- Braintrust is proprietary. Highbar is open source (MIT).

### vs. Manual Testing

Manual testing means running the agent and checking outputs by hand, often using chat interfaces or API clients.

**Where manual testing fits:** Exploratory testing, edge case discovery, UX evaluation.

**Where Highbar fits:** Regression testing, CI/CD integration, systematic guardrail validation.

**Key differences:**
- Manual testing does not scale and cannot run in CI.
- Manual testing cannot reliably verify function call arguments.
- Manual testing is good for discovering new test cases; Highbar is good for automating them.

## When to Use Highbar

Highbar is the right tool when you need to verify:

- An agent calls specific tools with specific arguments.
- Multi-turn workflows complete correctly with data flowing between turns.
- Guardrails hold against prompt injection, PII leakage, and scope violations.
- Agent behavior does not regress as you update prompts, models, or tool implementations.

Highbar is **not** a replacement for LLM evaluation tools. Use Promptfoo or DeepEval to evaluate your model, and Highbar to test your agent. They are complementary.

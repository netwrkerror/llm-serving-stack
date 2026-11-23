# Grading sheet

56 items: 8 reasoning rubrics x 2 tiers, plus `content` and `format` for 10 code
prompts x 2 tiers. Run `benchmark/out/run-eval-1bad7ca-20261116-174250`, eval set at
`1bad7ca`, `temperature=0`.

Fill **PASS** or **FAIL** and a one-sentence reason in the two blank fields under each
item. Nothing is pre-filled. The session's own grades are deliberately **not shown**
here, so this is an independent pass rather than a review of mine — `report.py` will
list the disagreements once both columns exist.

Each completion is reproduced in full, verbatim, including code fences. The
`sha256` is the first 16 characters of the hash `report.py` verifies.

---

## rubric — 16 items

**Question for every item below:** Does the answer contain what the rubric names?

### reason-03 · balanced · `llama3.1:8b` · rubric

- sha256: `ecd16226d68fffb8`
- prompt: Explain in one sentence why a semaphore that caps in-flight requests does not make a model batch them.
- rubric: **Says admission control limits concurrency but batching is the backend's/engine's decision; does not claim the semaphore batches.**

<details><summary>completion</summary>

```text
A semaphore that caps in-flight requests prevents the model from batching by limiting the number of concurrent requests, rather than allowing it to process multiple requests simultaneously as batching would.
```

</details>

- [ ] verdict: `FAIL   ` &nbsp; <!-- PASS or FAIL -->
- reason: ` Says the semaphore prevents batchin; doesn't connect with backend/engine's decision                         `

### reason-03 · fast · `llama3.2:1b` · rubric

- sha256: `bfe79d230af4e330`
- prompt: Explain in one sentence why a semaphore that caps in-flight requests does not make a model batch them.
- rubric: **Says admission control limits concurrency but batching is the backend's/engine's decision; does not claim the semaphore batches.**

<details><summary>completion</summary>

```text
A semaphore that caps in-flight requests prevents it from making batches, as the request is already being processed and cannot be added to a queue or batched until it has completed its current processing cycle.
```

</details>

- [ ] verdict: `    FAIL     ` &nbsp; <!-- PASS or FAIL -->
- reason: `  wrong claim;the semaphore stops batches from forming        `

### reason-04 · balanced · `llama3.1:8b` · rubric

- sha256: `b7069f3d6c5f9a11`
- prompt: You measure p50 latency at 100ms and p99 at 4s. In one sentence, what does that gap suggest about the workload?
- rubric: **Identifies a long tail / variance / queueing or contention affecting a minority of requests, rather than claiming the typical request is slow.**

<details><summary>completion</summary>

```text
The large gap between a p50 latency of 100ms and a p99 latency of 4 seconds suggests that the workload has a long tail of slow responses, indicating potential issues with resource contention, blocking operations, or other bottlenecks that are only occasionally triggered.
```

</details>

- [ ] verdict: ` PASS               ` &nbsp; <!-- PASS or FAIL -->
- reason: ` Long tail from contention that is - only occasionally triggered    `

### reason-04 · fast · `llama3.2:1b` · rubric

- sha256: `416878663512f601`
- prompt: You measure p50 latency at 100ms and p99 at 4s. In one sentence, what does that gap suggest about the workload?
- rubric: **Identifies a long tail / variance / queueing or contention affecting a minority of requests, rather than claiming the typical request is slow.**

<details><summary>completion</summary>

```text
The significant gap between the measured p50 latency of 100ms and the p99 latency of 4 seconds suggests that the workload is likely to be very high-priority or time-critical, as it indicates a substantial amount of time is being spent on tasks that are not critical to immediate response times.
```

</details>

- [ ] verdict: `   FAIL   ` &nbsp; <!-- PASS or FAIL -->
- reason: `  reads the gap as high-priority work, not a tail     `

### reason-05 · balanced · `llama3.1:8b` · rubric

- sha256: `b9be8d719e74d172`
- prompt: Is it cheaper to retry a failed request against a second paid provider, or to return the error? Answer in one sentence, naming the case where retrying is wasted.
- rubric: **Notes that retrying a request-side error (bad input, unknown model, oversized context) just pays twice for the same failure.**

<details><summary>completion</summary>

```text
Retrying a failed request against a second paid provider can be cheaper than returning the error if the failure is due to a temporary network issue, but it's wasted effort if the failure is caused by a permanent problem with the first provider's infrastructure or a fundamental incompatibility between the request and the provider's capabilities.
```

</details>

- [ ] verdict: `  FAIL   ` &nbsp; <!-- PASS or FAIL -->
- reason: `  says retrying is wasted on a provider infra failure, which is exactly when retrying helps; never names request-side errors      `

### reason-05 · fast · `llama3.2:1b` · rubric

- sha256: `1e91b0283525ca73`
- prompt: Is it cheaper to retry a failed request against a second paid provider, or to return the error? Answer in one sentence, naming the case where retrying is wasted.
- rubric: **Notes that retrying a request-side error (bad input, unknown model, oversized context) just pays twice for the same failure.**

<details><summary>completion</summary>

```text
It's generally more cost-effective to return the error rather than retrying a failed request against a second paid provider, as this approach can lead to additional charges for each failed attempt, whereas returning the error typically incurs no additional costs.
```

</details>

- [ ] verdict: `  FAIL         ` &nbsp; <!-- PASS or FAIL -->
- reason: `       naver names a case where retrying is wasted            `

### reason-06 · balanced · `llama3.1:8b` · rubric

- sha256: `4e533ff41c786f24`
- prompt: Explain in one sentence why time-to-first-token can be high while tokens-per-second is also high.
- rubric: **Separates prefill/queue wait from decode throughput: the wait precedes generation, which can still be fast.**

<details><summary>completion</summary>

```text
Time-to-first-token can be high because it measures the delay between a request being sent and the first token being received, whereas tokens-per-second measures the rate at which tokens are processed once they start arriving, so even if tokens are being processed quickly, there may still be a significant delay before any tokens arrive.
```

</details>

- [ ] verdict: ` PASS   ` &nbsp; <!-- PASS or FAIL -->
- reason: `   separates the delay before the first token from the rate once tokens arrive   `

### reason-06 · fast · `llama3.2:1b` · rubric

- sha256: `c9a164b02c3b1437`
- prompt: Explain in one sentence why time-to-first-token can be high while tokens-per-second is also high.
- rubric: **Separates prefill/queue wait from decode throughput: the wait precedes generation, which can still be fast.**

<details><summary>completion</summary>

```text
Time-to-first-token (TTF) can be high due to the overhead of tokenization, where the system spends more time processing each input token before it can be used for further processing or analysis.
```

</details>

- [ ] verdict: `       FAIL ` &nbsp; <!-- PASS or FAIL -->
- reason: `     blames tokenization overhead; never mentions throughput  `

### reason-07 · balanced · `llama3.1:8b` · rubric

- sha256: `7aedc22acb2f2125`
- prompt: A 1B model and an 8B model are given the same prompt and report different prompt token counts. Give one plausible cause in one sentence.
- rubric: **Names a different chat template/prompt formatting or tokenizer/vocabulary as the cause.**

<details><summary>completion</summary>

```text
One plausible cause is that the 1B model uses a more aggressive truncation strategy to limit its input length, whereas the 8B model has a larger capacity and can handle longer inputs without truncating them.
```

</details>

- [ ] verdict: `        FAIL        ` &nbsp; <!-- PASS or FAIL -->
- reason: `          Blames truncation strategy, not the template or tokenizer.             `

### reason-07 · fast · `llama3.2:1b` · rubric

- sha256: `6b851a30a9bb50a3`
- prompt: A 1B model and an 8B model are given the same prompt and report different prompt token counts. Give one plausible cause in one sentence.
- rubric: **Names a different chat template/prompt formatting or tokenizer/vocabulary as the cause.**

<details><summary>completion</summary>

```text
One plausible cause for the difference in prompt token counts between the 1B and 8B models is that the 8B model, with its larger capacity, may be able to generate more complex and nuanced prompts due to its ability to handle longer input sequences and more abstract representations of language.
```

</details>

- [ ] verdict: `    FAIL            ` &nbsp; <!-- PASS or FAIL -->
- reason: `   blames model capacity                                      `

### reason-08 · balanced · `llama3.1:8b` · rubric

- sha256: `91e39b7441678c17`
- prompt: If a streaming client disconnects, why might the server keep generating? Answer in one sentence.
- rubric: **Says the server does not learn of the disconnect until it writes, or that the upstream request/connection is not cancelled, so generation continues.**

<details><summary>completion</summary>

```text
The server may continue to generate new content or data even after a client disconnects if it is designed to operate independently of client connections and has a built-in scheduling mechanism for content delivery.
```

</details>

- [ ] verdict: `    FAIL   ` &nbsp; <!-- PASS or FAIL -->
- reason: `        Invents a scheduling mechanism; nothing about the disconnect going unnoticed or the upstream not being cancelled    `

### reason-08 · fast · `llama3.2:1b` · rubric

- sha256: `cbfb6541b3aec3c8`
- prompt: If a streaming client disconnects, why might the server keep generating? Answer in one sentence.
- rubric: **Says the server does not learn of the disconnect until it writes, or that the upstream request/connection is not cancelled, so generation continues.**

<details><summary>completion</summary>

```text
The server may continue to generate content because it has already sent the initial data or metadata for the stream, and the client's disconnection is simply an acknowledgement of the request, allowing the server to proceed with processing any remaining requests.
```

</details>

- [ ] verdict: `     FAIL ` &nbsp; <!-- PASS or FAIL -->
- reason: `    Treats the disconnect as an acknowledgement      `

### reason-09 · balanced · `llama3.1:8b` · rubric

- sha256: `a7cc1e71ae851685`
- prompt: Explain in one sentence the difference between a token and a word.
- rubric: **Says a token is a sub-word unit produced by a tokenizer, so one word can be several tokens.**

<details><summary>completion</summary>

```text
A token is a single unit of text, such as a punctuation mark or a space, whereas a word is a sequence of tokens that forms a meaningful unit of language.
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `my verdict could have been FAIL; however since it mentioned that word is a sequence of tokens that forms a meaningful unit... I passed it based on reasoning`

### reason-09 · fast · `llama3.2:1b` · rubric

- sha256: `e771bbb993ae5d0a`
- prompt: Explain in one sentence the difference between a token and a word.
- rubric: **Says a token is a sub-word unit produced by a tokenizer, so one word can be several tokens.**

<details><summary>completion</summary>

```text
A token is a unit of meaning or a symbol that represents a word, phrase, or concept, whereas a word is a spoken or written expression that has a specific meaning and conveys a particular idea or concept.
```

</details>

- [ ] verdict: `FAIL` &nbsp; <!-- PASS or FAIL -->
- reason: `Defines a token as a unit of meaning`

### reason-10 · balanced · `llama3.1:8b` · rubric

- sha256: `cc3f2f262fe1f5a5`
- prompt: Which costs more to serve: 1000 requests of 10 output tokens each, or 10 requests of 1000 output tokens each? Answer in one sentence, assuming per-token pricing.
- rubric: **Notes output token totals are equal (10,000) so per-token cost is the same, while per-request overheads favour fewer, larger requests.**

<details><summary>completion</summary>

```text
The two scenarios cost the same to serve, as they both require serving 10,000 output tokens.
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `passing it as it's a loaded question on serving cost; answers equal at 10,000 output tokens`

### reason-10 · fast · `llama3.2:1b` · rubric

- sha256: `9479c23ce63c46d3`
- prompt: Which costs more to serve: 1000 requests of 10 output tokens each, or 10 requests of 1000 output tokens each? Answer in one sentence, assuming per-token pricing.
- rubric: **Notes output token totals are equal (10,000) so per-token cost is the same, while per-request overheads favour fewer, larger requests.**

<details><summary>completion</summary>

```text
To determine which option costs more to serve, we need to calculate the total cost for both scenarios. Assuming a fixed price per token, serving 1000 requests of 10 output tokens each would be $10,000 (1000 * 10), while serving 10 requests of 1000 output tokens each would be $1,000,000. Therefore, serving 1000 requests of 10 output tokens each costs more to serve.
```

</details>

- [ ] verdict: `FAIL` &nbsp; <!-- PASS or FAIL -->
- reason: `lol made up $10,000 vs $1,000,000 for equal totals`

---

## content — 20 items

**Question for every item below:** Is the substantive answer right? (ignore whether it obeyed the output instruction)

### code-01 · balanced · `llama3.1:8b` · content

- sha256: `a83a5fdcd628fbc5`
- prompt: Write a one-line Python list comprehension that returns the squares of 1 through 5. Output only the code.
- committed check (regex): `for\s+\w+\s+in\s+range\s*\(\s*1\s*,\s*6\s*\)`
- completion tokens: 18

<details><summary>completion</summary>

```text
`result = [x**2 for x in range(1,6)]`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `range(1,6) comprehension`

### code-01 · fast · `llama3.2:1b` · content

- sha256: `7951307fcbe0a589`
- prompt: Write a one-line Python list comprehension that returns the squares of 1 through 5. Output only the code.
- committed check (regex): `for\s+\w+\s+in\s+range\s*\(\s*1\s*,\s*6\s*\)`
- completion tokens: 30

<details><summary>completion</summary>

```text
```python
[1**2, 2**2, 3**2, 4**2, 5**2]
```
```

</details>

- [ ] verdict: `FAIL` &nbsp; <!-- PASS or FAIL -->
- reason: `a literal list, not a comprehension`

### code-02 · balanced · `llama3.1:8b` · content

- sha256: `6e5750483eda1f89`
- prompt: Write a Python function signature (no body) named add that takes two ints and returns an int, with type hints. Output only the code.
- committed check (regex): `def\s+add\s*\(\s*\w+\s*:\s*int\s*,\s*\w+\s*:\s*int\s*\)\s*->\s*int`
- completion tokens: 21

<details><summary>completion</summary>

```text
```python
def add(a: int, b: int) -> int:
    pass
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `right signature; pass is the minimum Python allows*`

### code-02 · fast · `llama3.2:1b` · content

- sha256: `2924c3081287576a`
- prompt: Write a Python function signature (no body) named add that takes two ints and returns an int, with type hints. Output only the code.
- committed check (regex): `def\s+add\s*\(\s*\w+\s*:\s*int\s*,\s*\w+\s*:\s*int\s*\)\s*->\s*int`
- completion tokens: 61

<details><summary>completion</summary>

```text
```python
from typing import Add

def add(a: int, b: int) -> int:
```

Note: In Python 3.8+, you can use the built-in `Add` class from the `typing` module to specify the return type of a function with no body.
```

</details>

- [ ] verdict: `FAIL` &nbsp; <!-- PASS or FAIL -->
- reason: `from typing import Add doesn't exist, so the code doesn't import`

### code-03 · balanced · `llama3.1:8b` · content

- sha256: `14ef322f497d7ff8`
- prompt: Return a JSON object with exactly two keys, "name" set to "gateway" and "port" set to 8000. Output only JSON.
- committed check (json): `{"name": "gateway", "port": 8000}`
- completion tokens: 113

<details><summary>completion</summary>

```text
```python
import json

def return_json():
    """
    Returns a JSON object with 'name' as 'gateway' and 'port' as 8000.
    
    Returns:
        str: A JSON string containing the specified key-value pairs.
    """

    # Create a dictionary to hold our data
    data = {
        "name": "gateway",
        "port": 8000
    }

    # Convert the dictionary into a JSON object
    json_object = json.dumps(data)

    return json_object

print(return_json())
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `the code produces the right object`

### code-03 · fast · `llama3.2:1b` · content

- sha256: `154fc4a05d1d1db3`
- prompt: Return a JSON object with exactly two keys, "name" set to "gateway" and "port" set to 8000. Output only JSON.
- committed check (json): `{"name": "gateway", "port": 8000}`
- completion tokens: 22

<details><summary>completion</summary>

```text
```json
{
  "name": "gateway",
  "port": 8000
}
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `right object; clever compared to balanced which wrote the python script to generate the object`

### code-04 · balanced · `llama3.1:8b` · content

- sha256: `9d769268d084c6b4`
- prompt: Write the curl flag that follows redirects. Output only the flag.
- committed check (regex): `(?:^|\s)(-L|--location)\b`
- completion tokens: 5

<details><summary>completion</summary>

```text
`-L`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `-L`

### code-04 · fast · `llama3.2:1b` · content

- sha256: `8eb5ed7b78da4bc7`
- prompt: Write the curl flag that follows redirects. Output only the flag.
- committed check (regex): `(?:^|\s)(-L|--location)\b`
- completion tokens: 3

<details><summary>completion</summary>

```text
--r
```

</details>

- [ ] verdict: `FAIL` &nbsp; <!-- PASS or FAIL -->
- reason: `--r is not a curl flag`

### code-05 · balanced · `llama3.1:8b` · content

- sha256: `abd2b549278f0680`
- prompt: Write a Python dict literal mapping the string 'a' to 1 and 'b' to 2. Output only the code.
- committed check (regex): `\{\s*['"]a['"]\s*:\s*1\s*,\s*['"]b['"]\s*:\s*2\s*\}`
- completion tokens: 14

<details><summary>completion</summary>

```text
`{'a': 1, 'b': 2}`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-05 · fast · `llama3.2:1b` · content

- sha256: `6e66ae1ed0107b16`
- prompt: Write a Python dict literal mapping the string 'a' to 1 and 'b' to 2. Output only the code.
- committed check (regex): `\{\s*['"]a['"]\s*:\s*1\s*,\s*['"]b['"]\s*:\s*2\s*\}`
- completion tokens: 16

<details><summary>completion</summary>

```text
```
{'a': 1, 'b': 2}
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-06 · balanced · `llama3.1:8b` · content

- sha256: `35d2c3ea3d8bba2c`
- prompt: Write a regular expression that matches a string of exactly four digits. Output only the regex.
- committed check (regex): `\\d\{4\}|\[0-9\]\{4\}`
- completion tokens: 6

<details><summary>completion</summary>

```text
`\d{4}`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `\d{4} without ^…$ also matches inside "12345". Both tiers gave the same answer, so it can't change the tier comparison. It only changes both totals by one.`

### code-06 · fast · `llama3.2:1b` · content

- sha256: `c6f991731f9e0fb9`
- prompt: Write a regular expression that matches a string of exactly four digits. Output only the regex.
- committed check (regex): `\\d\{4\}|\[0-9\]\{4\}`
- completion tokens: 8

<details><summary>completion</summary>

```text
```
\d{4}
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `\d{4} without ^…$ also matches inside "12345". Both tiers gave the same answer, so it can't change the tier comparison. It only changes both totals by one.`

### code-07 · balanced · `llama3.1:8b` · content

- sha256: `587035849d260820`
- prompt: Return a JSON array containing the numbers 1, 2 and 3 in that order. Output only JSON.
- committed check (json): `[1, 2, 3]`
- completion tokens: 14

<details><summary>completion</summary>

```text
```json
[1, 2, 3]
```
```

</details>

- [ ] verdict: `       PASS         ` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-07 · fast · `llama3.2:1b` · content

- sha256: `fa0ad75a1851b35e`
- prompt: Return a JSON array containing the numbers 1, 2 and 3 in that order. Output only JSON.
- committed check (json): `[1, 2, 3]`
- completion tokens: 18

<details><summary>completion</summary>

```text
```
[
  1,
  2,
  3
]
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-08 · balanced · `llama3.1:8b` · content

- sha256: `589bae933f7cf7ec`
- prompt: Write the git command that shows the commit log in one line per commit. Output only the command.
- committed check (regex): `git\s+log[^\n]*--oneline`
- completion tokens: 10

<details><summary>completion</summary>

```text
`git log --format=oneline`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `--format=oneline prints one line per commit`

### code-08 · fast · `llama3.2:1b` · content

- sha256: `7cbe391ef5b97adb`
- prompt: Write the git command that shows the commit log in one line per commit. Output only the command.
- committed check (regex): `git\s+log[^\n]*--oneline`
- completion tokens: 9

<details><summary>completion</summary>

```text
`git log --format=%s`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `passing it; --format=%s prints one line per commit (subject only)`

### code-09 · balanced · `llama3.1:8b` · content

- sha256: `a9e875ebea525961`
- prompt: Write a Python expression that reads the environment variable PORT with a default of 8000. Output only the code.
- committed check (regex): `environ(?:\.get\(\s*['"]PORT['"]\s*,|\[\s*['"]PORT['"]\s*\])|getenv\(\s*['"]PORT['"]`
- completion tokens: 16

<details><summary>completion</summary>

```text
`PORT = int(os.environ.get('PORT', '8000'))`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `environ.get('PORT','8000'), int-cast`

### code-09 · fast · `llama3.2:1b` · content

- sha256: `cb03734f11d8d4cd`
- prompt: Write a Python expression that reads the environment variable PORT with a default of 8000. Output only the code.
- committed check (regex): `environ(?:\.get\(\s*['"]PORT['"]\s*,|\[\s*['"]PORT['"]\s*\])|getenv\(\s*['"]PORT['"]`
- completion tokens: 19

<details><summary>completion</summary>

```text
```python
import os

print(os.environ.get('PORT', '8000'))
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `same read; wrapped in print`

### code-10 · balanced · `llama3.1:8b` · content

- sha256: `f4ae7b0bef77227e`
- prompt: Return a JSON object with a key "ok" whose value is the boolean true. Output only JSON.
- committed check (json): `{"ok": true}`
- completion tokens: 77

<details><summary>completion</summary>

```text
You can use the following Python code to achieve this:

```python
import json

def return_ok_json():
    data = {"ok": True}
    return json.dumps(data)

print(return_ok_json())
```

This will output:
```
{"ok":true}
```

Note: The `json.dumps()` function is used to convert a Python object into a JSON string.
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `the code yields {"ok":true}`

### code-10 · fast · `llama3.2:1b` · content

- sha256: `3d0fc6e0ffac9304`
- prompt: Return a JSON object with a key "ok" whose value is the boolean true. Output only JSON.
- committed check (json): `{"ok": true}`
- completion tokens: 12

<details><summary>completion</summary>

```text
```
{
  "ok": true
}
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

---

## format — 20 items

**Question for every item below:** Did it obey the output instruction? (ignore whether the answer is right)

### code-01 · balanced · `llama3.1:8b` · format

- sha256: `a83a5fdcd628fbc5`
- prompt: Write a one-line Python list comprehension that returns the squares of 1 through 5. Output only the code.
- committed check (regex): `for\s+\w+\s+in\s+range\s*\(\s*1\s*,\s*6\s*\)`
- completion tokens: 18

<details><summary>completion</summary>

```text
`result = [x**2 for x in range(1,6)]`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-01 · fast · `llama3.2:1b` · format

- sha256: `7951307fcbe0a589`
- prompt: Write a one-line Python list comprehension that returns the squares of 1 through 5. Output only the code.
- committed check (regex): `for\s+\w+\s+in\s+range\s*\(\s*1\s*,\s*6\s*\)`
- completion tokens: 30

<details><summary>completion</summary>

```text
```python
[1**2, 2**2, 3**2, 4**2, 5**2]
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-02 · balanced · `llama3.1:8b` · format

- sha256: `6e5750483eda1f89`
- prompt: Write a Python function signature (no body) named add that takes two ints and returns an int, with type hints. Output only the code.
- committed check (regex): `def\s+add\s*\(\s*\w+\s*:\s*int\s*,\s*\w+\s*:\s*int\s*\)\s*->\s*int`
- completion tokens: 21

<details><summary>completion</summary>

```text
```python
def add(a: int, b: int) -> int:
    pass
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-02 · fast · `llama3.2:1b` · format

- sha256: `2924c3081287576a`
- prompt: Write a Python function signature (no body) named add that takes two ints and returns an int, with type hints. Output only the code.
- committed check (regex): `def\s+add\s*\(\s*\w+\s*:\s*int\s*,\s*\w+\s*:\s*int\s*\)\s*->\s*int`
- completion tokens: 61

<details><summary>completion</summary>

```text
```python
from typing import Add

def add(a: int, b: int) -> int:
```

Note: In Python 3.8+, you can use the built-in `Add` class from the `typing` module to specify the return type of a function with no body.
```

</details>

- [ ] verdict: `FAIL` &nbsp; <!-- PASS or FAIL -->
- reason: `prose "Note:" after the code`

### code-03 · balanced · `llama3.1:8b` · format

- sha256: `14ef322f497d7ff8`
- prompt: Return a JSON object with exactly two keys, "name" set to "gateway" and "port" set to 8000. Output only JSON.
- committed check (json): `{"name": "gateway", "port": 8000}`
- completion tokens: 113

<details><summary>completion</summary>

```text
```python
import json

def return_json():
    """
    Returns a JSON object with 'name' as 'gateway' and 'port' as 8000.
    
    Returns:
        str: A JSON string containing the specified key-value pairs.
    """

    # Create a dictionary to hold our data
    data = {
        "name": "gateway",
        "port": 8000
    }

    # Convert the dictionary into a JSON object
    json_object = json.dumps(data)

    return json_object

print(return_json())
```
```

</details>

- [ ] verdict: `FAIL` &nbsp; <!-- PASS or FAIL -->
- reason: `Python program, not JSON`

### code-03 · fast · `llama3.2:1b` · format

- sha256: `154fc4a05d1d1db3`
- prompt: Return a JSON object with exactly two keys, "name" set to "gateway" and "port" set to 8000. Output only JSON.
- committed check (json): `{"name": "gateway", "port": 8000}`
- completion tokens: 22

<details><summary>completion</summary>

```text
```json
{
  "name": "gateway",
  "port": 8000
}
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-04 · balanced · `llama3.1:8b` · format

- sha256: `9d769268d084c6b4`
- prompt: Write the curl flag that follows redirects. Output only the flag.
- committed check (regex): `(?:^|\s)(-L|--location)\b`
- completion tokens: 5

<details><summary>completion</summary>

```text
`-L`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-04 · fast · `llama3.2:1b` · format

- sha256: `8eb5ed7b78da4bc7`
- prompt: Write the curl flag that follows redirects. Output only the flag.
- committed check (regex): `(?:^|\s)(-L|--location)\b`
- completion tokens: 3

<details><summary>completion</summary>

```text
--r
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `(only a flag, a wrong one)`

### code-05 · balanced · `llama3.1:8b` · format

- sha256: `abd2b549278f0680`
- prompt: Write a Python dict literal mapping the string 'a' to 1 and 'b' to 2. Output only the code.
- committed check (regex): `\{\s*['"]a['"]\s*:\s*1\s*,\s*['"]b['"]\s*:\s*2\s*\}`
- completion tokens: 14

<details><summary>completion</summary>

```text
`{'a': 1, 'b': 2}`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-05 · fast · `llama3.2:1b` · format

- sha256: `6e66ae1ed0107b16`
- prompt: Write a Python dict literal mapping the string 'a' to 1 and 'b' to 2. Output only the code.
- committed check (regex): `\{\s*['"]a['"]\s*:\s*1\s*,\s*['"]b['"]\s*:\s*2\s*\}`
- completion tokens: 16

<details><summary>completion</summary>

```text
```
{'a': 1, 'b': 2}
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-06 · balanced · `llama3.1:8b` · format

- sha256: `35d2c3ea3d8bba2c`
- prompt: Write a regular expression that matches a string of exactly four digits. Output only the regex.
- committed check (regex): `\\d\{4\}|\[0-9\]\{4\}`
- completion tokens: 6

<details><summary>completion</summary>

```text
`\d{4}`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-06 · fast · `llama3.2:1b` · format

- sha256: `c6f991731f9e0fb9`
- prompt: Write a regular expression that matches a string of exactly four digits. Output only the regex.
- committed check (regex): `\\d\{4\}|\[0-9\]\{4\}`
- completion tokens: 8

<details><summary>completion</summary>

```text
```
\d{4}
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-07 · balanced · `llama3.1:8b` · format

- sha256: `587035849d260820`
- prompt: Return a JSON array containing the numbers 1, 2 and 3 in that order. Output only JSON.
- committed check (json): `[1, 2, 3]`
- completion tokens: 14

<details><summary>completion</summary>

```text
```json
[1, 2, 3]
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-07 · fast · `llama3.2:1b` · format

- sha256: `fa0ad75a1851b35e`
- prompt: Return a JSON array containing the numbers 1, 2 and 3 in that order. Output only JSON.
- committed check (json): `[1, 2, 3]`
- completion tokens: 18

<details><summary>completion</summary>

```text
```
[
  1,
  2,
  3
]
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-08 · balanced · `llama3.1:8b` · format

- sha256: `589bae933f7cf7ec`
- prompt: Write the git command that shows the commit log in one line per commit. Output only the command.
- committed check (regex): `git\s+log[^\n]*--oneline`
- completion tokens: 10

<details><summary>completion</summary>

```text
`git log --format=oneline`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-08 · fast · `llama3.2:1b` · format

- sha256: `7cbe391ef5b97adb`
- prompt: Write the git command that shows the commit log in one line per commit. Output only the command.
- committed check (regex): `git\s+log[^\n]*--oneline`
- completion tokens: 9

<details><summary>completion</summary>

```text
`git log --format=%s`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-09 · balanced · `llama3.1:8b` · format

- sha256: `a9e875ebea525961`
- prompt: Write a Python expression that reads the environment variable PORT with a default of 8000. Output only the code.
- committed check (regex): `environ(?:\.get\(\s*['"]PORT['"]\s*,|\[\s*['"]PORT['"]\s*\])|getenv\(\s*['"]PORT['"]`
- completion tokens: 16

<details><summary>completion</summary>

```text
`PORT = int(os.environ.get('PORT', '8000'))`
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: ``

### code-09 · fast · `llama3.2:1b` · format

- sha256: `cb03734f11d8d4cd`
- prompt: Write a Python expression that reads the environment variable PORT with a default of 8000. Output only the code.
- committed check (regex): `environ(?:\.get\(\s*['"]PORT['"]\s*,|\[\s*['"]PORT['"]\s*\])|getenv\(\s*['"]PORT['"]`
- completion tokens: 19

<details><summary>completion</summary>

```text
```python
import os

print(os.environ.get('PORT', '8000'))
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

### code-10 · balanced · `llama3.1:8b` · format

- sha256: `f4ae7b0bef77227e`
- prompt: Return a JSON object with a key "ok" whose value is the boolean true. Output only JSON.
- committed check (json): `{"ok": true}`
- completion tokens: 77

<details><summary>completion</summary>

```text
You can use the following Python code to achieve this:

```python
import json

def return_ok_json():
    data = {"ok": True}
    return json.dumps(data)

print(return_ok_json())
```

This will output:
```
{"ok":true}
```

Note: The `json.dumps()` function is used to convert a Python object into a JSON string.
```

</details>

- [ ] verdict: `FAIL` &nbsp; <!-- PASS or FAIL -->
- reason: `prose plus Python`

### code-10 · fast · `llama3.2:1b` · format

- sha256: `3d0fc6e0ffac9304`
- prompt: Return a JSON object with a key "ok" whose value is the boolean true. Output only JSON.
- committed check (json): `{"ok": true}`
- completion tokens: 12

<details><summary>completion</summary>

```text
```
{
  "ok": true
}
```
```

</details>

- [ ] verdict: `PASS` &nbsp; <!-- PASS or FAIL -->
- reason: `                                                                `

---

## When you are done

Either edit `eval/grades.jsonl` directly — the 56 lines with `"grader": "author"`,
setting `passed` to `true`/`false` and `reason` to your sentence — or hand this sheet
back filled in and I will transcribe it without changing a verdict.

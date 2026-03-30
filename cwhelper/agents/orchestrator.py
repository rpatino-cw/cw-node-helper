"""Agent Orchestra — three primitives for agent-to-agent orchestration.

Zero cwhelper-specific imports. Portable to any project that provides
an ai_chat_fn callable with signature:
    ai_chat_fn(messages: list[dict], model: str, temperature: float,
               max_tokens: int) -> str

Primitives:
    Agent    — wraps a single AI call with a role and optional JSON output
    Pipeline — chains Agents so output of A feeds input of B
    AgentLoop — recursive agent that runs until a stop condition or max_turns
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable


# Type alias for the AI chat function signature
AiChatFn = Callable[[list[dict], str, float, int], str]


@dataclass
class Agent:
    """Single-purpose AI agent with a role and system prompt.

    Usage:
        agent = Agent(
            name="quiz",
            system_prompt="You generate quiz questions about Python code.",
            ai_chat_fn=my_chat_fn,
        )
        result = agent.run("Generate 3 questions about this code: ...")
    """
    name: str
    system_prompt: str
    ai_chat_fn: AiChatFn
    model: str = ""
    temperature: float = 0.3
    max_tokens: int = 1024
    json_output: bool = False

    def run(self, user_input: str, context: dict | None = None) -> str | dict:
        """Execute a single AI call and return the response.

        Args:
            user_input: The user/task message to send.
            context: Optional dict merged into the user message as JSON context.

        Returns:
            str if json_output is False, parsed dict/list if json_output is True.
            Falls back to raw string if JSON parsing fails.
        """
        messages = [{"role": "system", "content": self.system_prompt}]

        content = user_input
        if context:
            content += f"\n\n--- CONTEXT ---\n{json.dumps(context, indent=2)}"

        messages.append({"role": "user", "content": content})

        raw = self.ai_chat_fn(
            messages, self.model, self.temperature, self.max_tokens
        )

        if not self.json_output:
            return raw

        return _parse_json_response(raw)


@dataclass
class Pipeline:
    """Chain of Agents with structured JSON handoff.

    Output of Agent A becomes input context for Agent B.

    Usage:
        pipe = Pipeline(agents=[reader_agent, quiz_agent, validator_agent])
        result = pipe.run("Analyze this module: walkthrough.py")
    """
    agents: list[Agent]
    verbose: bool = False

    def run(self, initial_input: str) -> str | dict:
        """Execute agents in sequence, passing output forward.

        Returns the output of the final agent.
        """
        current_input = initial_input
        current_context = None

        for i, agent in enumerate(self.agents):
            if self.verbose:
                print(f"  [{i+1}/{len(self.agents)}] Running {agent.name}...")

            result = agent.run(current_input, context=current_context)

            # Prepare for next agent
            if isinstance(result, dict):
                current_context = result
                current_input = f"Previous agent ({agent.name}) output is in CONTEXT."
            else:
                current_context = None
                current_input = f"Previous agent ({agent.name}) said:\n{result}"

        return result


@dataclass
class AgentLoop:
    """Recursive agent that runs until a stop condition or max_turns.

    The agent receives its own previous output plus user feedback each turn.
    Useful for iterative refinement (Feature Lab, brainstorming).

    Usage:
        loop = AgentLoop(
            agent=idea_agent,
            max_turns=3,
            stop_phrase="DONE",
        )
        # get_feedback is called each turn with the agent's response
        result = loop.run("Brainstorm features for walkthrough mode",
                          get_feedback=lambda resp: input("Your thoughts: "))
    """
    agent: Agent
    max_turns: int = 5
    stop_phrase: str = "DONE"
    verbose: bool = False

    def run(
        self,
        initial_input: str,
        get_feedback: Callable[[str], str | None] | None = None,
    ) -> list[dict]:
        """Execute the agent loop.

        Args:
            initial_input: The starting prompt.
            get_feedback: Callable that receives agent response and returns
                user feedback string, or None to stop the loop.

        Returns:
            List of conversation turns: [{"role": "agent"|"user", "content": ...}]
        """
        history: list[dict] = []
        current_input = initial_input

        for turn in range(self.max_turns):
            if self.verbose:
                print(f"  [Turn {turn+1}/{self.max_turns}]")

            response = self.agent.run(current_input)
            history.append({"role": "agent", "content": response})

            # Check for stop phrase
            if isinstance(response, str) and self.stop_phrase in response:
                break

            # Get user feedback if callback provided
            if get_feedback is None:
                break

            feedback = get_feedback(response)
            if feedback is None or feedback.strip() == "":
                break

            history.append({"role": "user", "content": feedback})
            current_input = (
                f"Your previous response:\n{response}\n\n"
                f"User feedback:\n{feedback}\n\n"
                "Continue based on the feedback."
            )

        return history


def _parse_json_response(raw: str) -> dict | list | str:
    """Extract JSON from an AI response, handling markdown code fences."""
    # Try direct parse first
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass

    # Try extracting from ```json ... ``` fences
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            pass

    # Try finding first { or [ to end of matching bracket
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = raw.find(start_char)
        if start >= 0:
            end = raw.rfind(end_char)
            if end > start:
                try:
                    return json.loads(raw[start:end + 1])
                except (json.JSONDecodeError, TypeError):
                    pass

    # Give up, return raw string
    return raw

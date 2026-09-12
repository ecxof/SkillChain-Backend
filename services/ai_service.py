from openai import AsyncOpenAI
import json
import os
from typing import List

_client = AsyncOpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)


async def analyze_project(
    repo_data: dict,
    skills_claimed: List[str],
    role_in_project: str
) -> dict:
    """Send repo data to Grok and get back a structured skill verification report."""

    prompt = f"""You are a technical skills verifier for a developer portfolio platform called SkillChain.

Your job is to analyze a GitHub repository and determine whether the developer's claimed skills are genuinely demonstrated by the code and project structure.

## Repository Info
- Name: {repo_data.get("name")}
- Description: {repo_data.get("description") or "None provided"}
- Languages: {json.dumps(repo_data.get("languages", {}))}
- Topics/Tags: {repo_data.get("topics", [])}
- Stars: {repo_data.get("stars")} | Forks: {repo_data.get("forks")}
- Commit Count: {repo_data.get("commit_count") or "Unknown"}
- Developer's Role: {role_in_project}

## README
{repo_data.get("readme_content") or "No README found."}

## Skills Claimed by Developer
{json.dumps(skills_claimed)}

## Instructions
For each claimed skill, assess:
1. Is it actually demonstrated in this project? (verified: true/false)
2. What level is shown? (beginner / intermediate / advanced)
3. Your confidence in this assessment (0-100)
4. A brief one-sentence reason

Then provide:
- overall_trust_score (0-100): How credible is the overall skill claim for this repo?
- summary: 2-3 sentences summarizing the project and your findings.

Respond ONLY with a valid JSON object, no explanation, no markdown, no backticks:
{{
  "skills": [
    {{
      "skill_name": "...",
      "verified": true,
      "level": "intermediate",
      "confidence": 85,
      "reason": "..."
    }}
  ],
  "overall_trust_score": 80,
  "summary": "..."
}}"""

    response = await _client.chat.completions.create(
        model="grok-3",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )

    raw = response.choices[0].message.content.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        clean = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(clean)
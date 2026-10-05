//! LLM client: a plain OpenAI-compatible `chat/completions` call, defaulting
//! to the Kimi Coding Plan endpoint (https://www.kimi.com/code/docs/en/) --
//! `OPENAI_BASE_URL` / `OPENAI_MODEL` override the default base URL/model,
//! and the key comes from `OPENAI_API_KEY` (or `KIMI_API_KEY`). Any other
//! OpenAI-compatible endpoint works the same way by setting those three.
//!
//! Ported call-for-call from the Python agent's `agent_core/llm_client.py`:
//! every call has a 10 s timeout, the whole run has a 300 s total LLM time
//! budget and a 100-call cap, a failing call is retried once (2 attempts
//! total), and a call that keeps failing returns `None` so that planning
//! step falls back to its rule-based answer for the night -- the next
//! scheduled call still runs normally.

use serde_json::{json, Value};
use std::env;
use std::time::{Duration, Instant};

const DEFAULT_BASE_URL: &str = "https://api.kimi.com/coding/v1";
const DEFAULT_MODEL: &str = "k3";
const CALL_TIMEOUT_SECONDS: f64 = 10.0;
const TOTAL_BUDGET_SECONDS: f64 = 300.0;
const MAX_CALLS: u32 = 100;
const MAX_RETRIES: u32 = 2;

pub struct LlmClient {
    base_url: String,
    api_key: String,
    model: String,
    pub calls_made: u32,
    pub spent_seconds: f64,
}

impl LlmClient {
    /// `Err` with a plain, user-facing message when no key is configured --
    /// callers are expected to log it and exit rather than start a run that
    /// cannot plan.
    pub fn from_env() -> Result<LlmClient, String> {
        let api_key = env::var("OPENAI_API_KEY")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .or_else(|| env::var("KIMI_API_KEY").ok().filter(|s| !s.trim().is_empty()))
            .map(|s| s.trim().to_string())
            .ok_or_else(|| "missing API key: set OPENAI_API_KEY".to_string())?;
        let base_url = env::var("OPENAI_BASE_URL")
            .ok()
            .map(|s| s.trim().trim_end_matches('/').to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| DEFAULT_BASE_URL.to_string());
        let model = env::var("OPENAI_MODEL")
            .ok()
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .unwrap_or_else(|| DEFAULT_MODEL.to_string());
        Ok(LlmClient { base_url, api_key, model, calls_made: 0, spent_seconds: 0.0 })
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    pub fn model(&self) -> &str {
        &self.model
    }

    /// Never let a model call eat into the last minute of wall clock, and
    /// never exceed this run's own LLM time allowance.
    fn budget_left(&self, wallclock_remaining_seconds: f64) -> f64 {
        CALL_TIMEOUT_SECONDS
            .min(TOTAL_BUDGET_SECONDS - self.spent_seconds)
            .min((wallclock_remaining_seconds - 60.0).max(0.0))
    }

    /// One HTTP attempt. `Err` on any problem; the caller retries or gives up.
    fn attempt(&self, system_prompt: &str, user_payload: &Value, timeout_seconds: f64) -> Result<Value, String> {
        let body = json!({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": serde_json::to_string(user_payload).unwrap_or_default()},
            ],
            "temperature": 0,
            "max_tokens": 250,
        });
        let url = format!("{}/chat/completions", self.base_url);
        let response = ureq::post(&url)
            .timeout(Duration::from_secs_f64(timeout_seconds.max(0.1)))
            .set("Content-Type", "application/json")
            .set("Authorization", &format!("Bearer {}", self.api_key))
            .send_json(body)
            .map_err(|e| format!("request failed ({e})"))?;
        let parsed: Value = response.into_json().map_err(|e| format!("response was not JSON ({e})"))?;
        let text = parsed
            .get("choices")
            .and_then(|c| c.get(0))
            .and_then(|c| c.get("message"))
            .and_then(|m| m.get("content"))
            .and_then(|c| c.as_str())
            .ok_or_else(|| "response had no choices[0].message.content".to_string())?;
        // Tolerate prose around the object: take the outermost {...} span.
        let start = text.find('{').ok_or_else(|| "no JSON object in model reply".to_string())?;
        let end = text.rfind('}').ok_or_else(|| "no JSON object in model reply".to_string())?;
        if end < start {
            return Err("no JSON object in model reply".to_string());
        }
        let parsed: Value = serde_json::from_str(&text[start..=end]).map_err(|e| format!("model reply was not valid JSON ({e})"))?;
        if !parsed.is_object() {
            return Err("model reply was not a JSON object".to_string());
        }
        Ok(parsed)
    }

    /// One planning question, answered as exactly one JSON object. Retries up
    /// to `MAX_RETRIES` times on failure; returns `None` once the budget/call
    /// cap/retries are exhausted, so the caller's rule-based answer takes over
    /// for this step.
    pub fn ask_json(&mut self, system_prompt: &str, user_payload: &Value, wallclock_remaining_seconds: f64) -> Option<Value> {
        let mut last_error = String::new();
        for _attempt in 0..MAX_RETRIES {
            if self.calls_made >= MAX_CALLS {
                crate::memory::log("llm: call cap reached for this run; using the rule-based path");
                return None;
            }
            let timeout = self.budget_left(wallclock_remaining_seconds);
            if timeout < 1.5 {
                crate::memory::log("llm: LLM time budget exhausted; using the rule-based path");
                return None;
            }
            let started = Instant::now();
            self.calls_made += 1;
            let result = self.attempt(system_prompt, user_payload, timeout);
            self.spent_seconds += started.elapsed().as_secs_f64();
            match result {
                Ok(value) => return Some(value),
                Err(reason) => last_error = reason,
            }
        }
        crate::memory::log(&format!(
            "llm: call failed after {MAX_RETRIES} attempts ({last_error}); this step falls back to its rule-based answer"
        ));
        None
    }
}

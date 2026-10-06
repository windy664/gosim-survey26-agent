//! The model-driven stages of the pro agent. Port of `python-pro/advisor.py`.
//! Two calls start at the beginning of every night:
//!
//! 1. night_plan   (natural-language understanding + plan adaptation): reads tonight's forecast and the current
//!    bulletin and decides whether tonight is a bad night for faint must-observe targets and which compass
//!    sectors to keep away from. The planner uses both answers for the whole night.
//! 2. fault_review (data parsing + action decision): reads the agent's own hour-by-hour quality table of the last
//!    nights and judges how likely an unannounced instrument fault is. The answer sets how readily the agent
//!    reports (probes) a fault tonight.
//!
//! A third, occasional call confirms a paid fault report before it is sent.
//!
//! Calls run in the background (llm_client::Call); the agent waits for them only as long as the wall clock allows
//! and keeps planning otherwise. Every answer is validated; a missing or invalid answer leaves the rule-based
//! value in place for that night.

use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use std::time::Instant;

use crate::llm_client::{Call, LlmClient};

const DIRECTIONS: [&str; 8] = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
const WEATHER_KINDS: [&str; 5] = ["rain", "storm", "overcast", "haze", "cold_snap"];

const NIGHT_PLAN_SYSTEM: &str = concat!(
    "You plan one night of a robotic spectroscopic survey. The input lists tonight's weather forecast notices and the ",
    "current bulletin; each notice is an event kind and a compass sector (N, NE, E, SE, S, SW, W, NW) or ALL (the ",
    "whole sky). Decide two things.\n",
    "bad_night: true when tonight's forecast or bulletin has rain, storm, overcast or haze over ALL of the sky. Faint ",
    "must-observe targets need a one-hour exposure in a clear sky, so on a bad night they should wait for a better ",
    "night.\n",
    "avoid_directions: the sectors with rain, storm, overcast, haze or cold_snap tonight. Ignore earthquake, ",
    "rocket_launch and terrain_obstruction (the scheduler handles those itself). Never list a sector nothing names.\n",
    "Reply with one JSON object only: {\"bad_night\": true|false, \"avoid_directions\": [\"SW\", ...], \"reason\": \"<12 words\"}"
);

const FAULT_REVIEW_SYSTEM: &str = concat!(
    "You watch the data quality of a robotic telescope. An instrument fault is never announced: it lowers the ",
    "instrument efficiency, and so the quality of every exposure, until someone reports it; a correct report ",
    "repairs it at once. An earthquake (it appears in the bulletin) also lowers instrument efficiency, and that loss ",
    "fades night by night; a report does not repair it. Weather lowers quality too, but it also lowers the program ",
    "band, which the instrument does not affect.\n",
    "Columns per hour: E = measured quality / quality the program bands allow (about 1 when healthy; low when the ",
    "instrument is the cause; in a very clear sky the bands bound it only loosely, so it can stay near 1), scale = ",
    "measured sky quality relative to the clear-sky model, ref = the usual scale since the last repair. ",
    "notices_now lists the current bulletin.\n",
    "Signs of a fault: quality that drops and stays down without recovering, E low for many hours across nights, ",
    "not explained by announced weather or by a recent earthquake whose effect is fading.\n",
    "Reporting: a correct report earns 100 and repairs the instrument; false reports are free while ",
    "free_false_reports_left > 0, afterwards each costs 150.\n",
    "Reply with one JSON object only: {\"fault_likely\": <0..1>, \"reason\": \"<15 words\"}"
);

const CONFIRM_SYSTEM: &str = concat!(
    "You check the evidence for an unannounced instrument fault on a robotic telescope before a paid report. A false ",
    "report costs 150 points; a correct one earns 100 and repairs the instrument. E per hour = measured quality / ",
    "quality the program bands allow: about 1 when healthy, low while the instrument is the cause. Weather lowers both ",
    "quality and band; an earthquake lowers instrument efficiency in a way that fades night by night and that a ",
    "report does not repair.\n",
    "Reply with one JSON object only: {\"report\": true|false, \"reason\": \"<15 words\"}"
);

const INTEL_SYSTEM: &str = concat!(
    "You are the logbook decoder for a robotic observatory. Observation requests carry handover-log notes in their ",
    "reason field. These notes hide TRUE site conditions that the official bulletins get wrong or omit. Lines may be ",
    "coded: Caesar cipher (shift letters BACK by the day-of-month of the note's date to read them; digits unchanged), ",
    "Morse code, and solfege numbers (do=1 re=2 mi=3 fa=4 sol=5 la=6 si=7, high do=8, high re=9, rest=0). Chinese, ",
    "Japanese and English may mix; full-width digits may appear. Later notes may correct earlier ones: the newest ",
    "correction wins. Many notes are gossip with no usable content; extract nothing from them.\n",
    "From all notes together, extract:\n",
    "terrain: the TRUE horizon altitude limit in degrees for a compass sector (N, NE, E, SE, S, SW, W, NW), only when ",
    "a note states how high a mountain or hill really blocks that direction (a number in degrees).\n",
    "maintenance: planned telescope downtime windows as exact UTC ranges, only when a note names a date and a time ",
    "range for a shutdown (coating check, repairs, power work). Convert every date/time to UTC ISO-8601.\n",
    "Reply with one JSON object only: ",
    "{\"terrain\": [{\"direction\": \"SW\", \"alt_deg\": 35.0}], ",
    "\"maintenance\": [{\"start_utc\": \"2027-01-06T23:15:00Z\", \"end_utc\": \"2027-01-07T01:45:00Z\"}], ",
    "\"notes\": \"<20 words>\"}",
    " Think briefly. Your ENTIRE reply must be that single JSON object: no prose, no code fence, no explanation."
);

/// True site conditions decoded from the request logbook notes.
pub struct Intel {
    pub terrain: Vec<(String, f64)>,
    pub maintenance: Vec<(f64, f64)>,
    pub notes: String,
}

pub struct NightPlan {
    pub bad_night: bool,
    pub avoid_directions: Vec<String>,
    pub reason: String,
}

pub struct FaultReview {
    pub fault_likely: f64,
    pub reason: String,
}

fn reason_of(answer: &Map<String, Value>) -> String {
    let text = match answer.get("reason") {
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
        None => String::new(),
    };
    text.chars().take(80).collect()
}

fn kind_dir(n: &Value) -> Value {
    json!({"event_kind": n.get("event_kind").cloned().unwrap_or(Value::Null),
           "direction": n.get("direction").cloned().unwrap_or(Value::Null)})
}

pub struct Advisor {
    plan_call: Option<Call>,
    fault_call: Option<Call>,
    intel_call: Option<Call>,
    plan_applied: bool,
    fault_applied: bool,
    intel_applied: bool,
    /// Some(true/false) once the intel call finishes, until the caller consumes it
    pub intel_outcome: Option<bool>,
    pub night_date: String,
    announced: BTreeSet<String>,
}

impl Advisor {
    pub fn new() -> Advisor {
        Advisor { plan_call: None, fault_call: None, intel_call: None, plan_applied: true, fault_applied: true, intel_applied: true, intel_outcome: None, night_date: String::new(), announced: BTreeSet::new() }
    }

    /// Fire the logbook-decoding call in the background; the answer lands on a later poll_intel.
    pub fn start_intel(&mut self, client: &mut LlmClient, texts: Value, wallclock_left: f64) {
        if self.intel_applied {
            self.intel_call = client.submit("intel", INTEL_SYSTEM, texts, wallclock_left, 8000);
            self.intel_applied = self.intel_call.is_none();
        }
    }

    /// The decoded site conditions, once the call finishes; None while pending or invalid.
    pub fn poll_intel(&mut self, client: &mut LlmClient) -> Option<Intel> {
        if self.intel_applied {
            return None;
        }
        let call = self.intel_call.as_mut()?;
        if !call.done() {
            return None;
        }
        self.intel_applied = true;
        let answer = client.collect(call);
        self.intel_outcome = Some(answer.is_some());
        let answer = answer?;
        let mut terrain = Vec::new();
        for t in answer.get("terrain").and_then(Value::as_array).into_iter().flatten() {
            let Some(d) = t.get("direction").and_then(Value::as_str) else { continue };
            let d = d.to_uppercase();
            let Some(a) = t.get("alt_deg").and_then(Value::as_f64) else { continue };
            if DIRECTIONS.contains(&d.as_str()) && (15.0..=60.0).contains(&a) {
                terrain.push((d, a));
            }
        }
        let mut maintenance = Vec::new();
        for m in answer.get("maintenance").and_then(Value::as_array).into_iter().flatten() {
            let (Some(s), Some(e)) = (
                m.get("start_utc").and_then(Value::as_str).map(crate::skymath::parse_utc),
                m.get("end_utc").and_then(Value::as_str).map(crate::skymath::parse_utc),
            ) else { continue };
            if e > s && e - s <= 5.0 * 86400.0 {
                maintenance.push((s, e));
            }
        }
        let notes: String = answer.get("notes").and_then(Value::as_str).unwrap_or("").chars().take(80).collect();
        Some(Intel { terrain, maintenance, notes })
    }

    /// Submit both calls; wait up to wait_seconds for them. Returns the (plan, fault) answers that are ready.
    #[allow(clippy::too_many_arguments)]
    pub fn start_night(&mut self, client: &mut LlmClient, night_date: &str, tonight: &[Value], bulletin: &[Value],
                       fault_table: Value, wallclock_left: f64, wait_seconds: f64) -> (Option<NightPlan>, Option<FaultReview>) {
        self.night_date = night_date.to_string();
        self.announced = tonight
            .iter()
            .chain(bulletin.iter())
            .filter(|n| n.get("event_kind").and_then(Value::as_str).map_or(false, |k| WEATHER_KINDS.contains(&k)))
            .filter_map(|n| n.get("direction").and_then(Value::as_str).map(str::to_string))
            .collect();
        let notices = json!({
            "night": night_date,
            "forecast_tonight": tonight.iter().map(kind_dir).collect::<Vec<_>>(),
            "bulletin_now": bulletin.iter().map(kind_dir).collect::<Vec<_>>(),
        });
        self.plan_call = client.submit("night_plan", NIGHT_PLAN_SYSTEM, notices, wallclock_left, 2000);
        self.fault_call = client.submit("fault_review", FAULT_REVIEW_SYSTEM, fault_table, wallclock_left, 2000);
        self.plan_applied = self.plan_call.is_none();
        self.fault_applied = self.fault_call.is_none();
        let deadline = Instant::now() + std::time::Duration::from_secs_f64(wait_seconds.max(0.0));
        for call in [&self.plan_call, &self.fault_call].into_iter().flatten() {
            call.wait(deadline.saturating_duration_since(Instant::now()).as_secs_f64());
        }
        self.poll(client)
    }

    /// (plan, fault) answers that arrived since the last poll; None for each one not (newly) available.
    pub fn poll(&mut self, client: &mut LlmClient) -> (Option<NightPlan>, Option<FaultReview>) {
        let mut plan = None;
        let mut fault = None;
        if !self.plan_applied {
            if let Some(call) = self.plan_call.as_mut() {
                if call.done() {
                    self.plan_applied = true;
                    let answer = client.collect(call);
                    plan = self.valid_plan(answer);
                }
            }
        }
        if !self.fault_applied {
            if let Some(call) = self.fault_call.as_mut() {
                if call.done() {
                    self.fault_applied = true;
                    fault = valid_fault(client.collect(call));
                }
            }
        }
        (plan, fault)
    }

    fn valid_plan(&self, answer: Option<Map<String, Value>>) -> Option<NightPlan> {
        let answer = answer?;
        let bad_night = answer.get("bad_night")?.as_bool()?;
        let avoid = match answer.get("avoid_directions") {
            None => Vec::new(),
            Some(Value::Array(a)) => a.clone(),
            Some(_) => return None,
        };
        // the model may rank announced weather; it may not close sky that nothing announced
        let avoid: BTreeSet<String> = avoid
            .iter()
            .map(|d| match d {
                Value::String(s) => s.to_uppercase(),
                other => other.to_string().to_uppercase(),
            })
            .filter(|d| DIRECTIONS.contains(&d.as_str()) && self.announced.contains(d))
            .collect();
        Some(NightPlan { bad_night, avoid_directions: avoid.into_iter().collect(), reason: reason_of(&answer) })
    }

    /// True / False from the model, or None (no answer in time: the rule decides).
    pub fn confirm_report(&mut self, client: &mut LlmClient, evidence: Value, wallclock_left: f64, wait_seconds: f64) -> Option<bool> {
        let mut call = client.submit("confirm_report", CONFIRM_SYSTEM, evidence, wallclock_left, 2000)?;
        call.wait(wait_seconds);
        let answer = client.collect(&mut call)?;
        answer.get("report").and_then(Value::as_bool)
    }
}

fn valid_fault(answer: Option<Map<String, Value>>) -> Option<FaultReview> {
    let answer = answer?;
    let p = match answer.get("fault_likely")? {
        Value::Number(n) => n.as_f64()?,
        Value::Bool(b) => f64::from(u8::from(*b)),
        Value::String(s) => s.trim().parse::<f64>().ok()?,
        _ => return None,
    };
    if !(0.0..=1.0).contains(&p) {
        return None;
    }
    Some(FaultReview { fault_likely: p, reason: reason_of(&answer) })
}

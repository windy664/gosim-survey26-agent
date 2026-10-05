//! Decision logic: pick a pointing, fill the 16 fibres, choose exposure
//! length and program -- or wait / report / finish.
//!
//! 1. Rank visible, not-yet-done targets. Required targets that are not done
//!    yet get a bonus (missing one costs real points at the end). Targets
//!    that set soon, or that have few nights left, rank higher.
//! 2. For the best few "anchor" candidates, try each fibre as the pointing
//!    centre; fill every fibre with the best-value neighbour that lands on
//!    its glass; keep the best pointing.
//! 3. Pick the exposure length with the best expected score per second, and
//!    the program (DARK / BRIGHT / BACKUP) most assigned targets will match.
//!
//! Everything here uses only the public catalogue, the public scoring config,
//! and the agent's own past hits (`memory::Memory`) -- never hidden weather
//! truth. Once per night, two LLM calls run and their answers are merged (see
//! `night_advice`): one reads the forecast/bulletin notices for tonight, the
//! other reads tonight's live bulletin text and the agent's own hit rate.
//! Their advice is parsed, evidence-checked and LOGGED ONLY -- never applied
//! -- so the decision sequence stays deterministic. A third, rarer LLM call
//! confirms a suspected instrument fault before a `report` fires.
//!
//! This is a feature-for-feature port of the Python agent's
//! `agent_core/planner.py` (the tuned M19 behaviour source), minus the parts
//! deliberately left behind: card_priors blind reports, k_corr, the
//! detector sandbox and the DEBUG_REPORT debug branch.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use serde_json::{json, Value};

use crate::llm::LlmClient;
use crate::memory::{log, Memory, PendingPrediction, RequestEntry, TraceLog};
use crate::protocol::{DecisionResponse, DecisionSnapshot, Pointing};
use crate::scoring::{self, Moon, SIDEREAL_DEG_PER_SECOND};
use crate::state::Config;

pub const REQUIRED_BONUS: f64 = 60.0;
const URGENT_REQUIRED_FLOOR: f64 = 5.0;
const DONE_FACTOR: f64 = 0.95;
const PLAN_FACTOR_SAFETY: f64 = 0.9;
const EDGE_MARGIN_DEG: f64 = 0.08;
const DURATIONS: [f64; 10] = [300.0, 450.0, 600.0, 900.0, 1200.0, 1500.0, 1800.0, 2400.0, 3000.0, 3600.0];
const MIN_VISIBLE_SECONDS: f64 = 600.0;
const NEIGHBOUR_RADIUS_DEG: f64 = 2.1;
const ANCHORS: usize = 6;
const ANCHOR_POOL: usize = 300;

const REPORT_DROP: f64 = 0.68;
const REPORT_CONFIRMATIONS: usize = 3;
const REPORT_SPACING_HOURS: f64 = 6.0;
// The organizers confirmed faults can happen more than once (at most one at a
// time). The first 2 reports are "exploration" -- even if both are wrong they
// are inside the free allowance (0 cost); only after a confirmed fault does
// the chase cap open up, so fault-free cards still pay 0 in the worst case
// and faulty cards never run out of budget before the season ends.
const MAX_REPORTS: u32 = 2;
const MAX_REPORTS_CONFIRMED: u32 = 6;

const FORECAST_SYSTEM_PROMPT: &str = "You help schedule a telescope survey. Reply with one JSON object only: \
    {\"avoid_directions\": [compass codes among N,NE,E,SE,S,SW,W,NW], \"duration_scale\": \
    number 0.85-1.4}. Avoid directions with bad weather tonight, going by the forecast \
    and the current bulletin; use a larger duration_scale when the sky looks poor.";
const BULLETIN_SYSTEM_PROMPT: &str = "You help schedule a telescope survey using tonight's live weather bulletin and the \
    agent's own recent hit rate. Reply with one JSON object only: {\"avoid_directions\": \
    [compass codes among N,NE,E,SE,S,SW,W,NW], \"duration_scale\": number 0.85-1.4}. Avoid \
    directions the bulletin text describes as closed or obstructed right now. Raise \
    duration_scale when the hit rate has been low (the sky has been performing poorly); \
    lower it when the hit rate has been high.";
const CONFIRM_SYSTEM_PROMPT: &str = "You check telescope data quality. A false instrument-fault report costs points, \
    a correct one earns points. Reply with one JSON object only: {\"report\": true|false}.";

/// Python's `round(x, 4)` (round-half-even), used for pointing coordinates.
fn round4(x: f64) -> f64 {
    (x * 10000.0).round_ties_even() / 10000.0
}

/// A human-readable rendering of a bulletin's notices, for the LLM call that
/// reads "live bulletin text" rather than structured JSON.
fn bulletin_text(notices: &[Value]) -> String {
    if notices.is_empty() {
        return "clear (no active notices)".to_string();
    }
    notices
        .iter()
        .map(|n| {
            format!(
                "{} {}",
                n.get("event_kind").and_then(|v| v.as_str()).unwrap_or(""),
                n.get("direction").and_then(|v| v.as_str()).unwrap_or("")
            )
        })
        .collect::<Vec<_>>()
        .join("; ")
}

/// Insertion-ordered fiber -> value map, mirroring Python dict semantics
/// (replacement keeps the original position). Iteration order feeds float
/// summations and the `pending` ring, so it must match the Python agent's
/// exactly; 16 fibres make a linear scan free.
struct FiberMap<V> {
    entries: Vec<(i64, V)>,
}

impl<V> FiberMap<V> {
    fn new() -> Self {
        FiberMap { entries: Vec::new() }
    }

    fn get(&self, key: i64) -> Option<&V> {
        self.entries.iter().find(|(k, _)| *k == key).map(|(_, v)| v)
    }

    fn set(&mut self, key: i64, value: V) {
        if let Some(slot) = self.entries.iter_mut().find(|(k, _)| *k == key) {
            slot.1 = value;
        } else {
            self.entries.push((key, value));
        }
    }

    fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    fn iter(&self) -> impl Iterator<Item = (i64, &V)> {
        self.entries.iter().map(|(k, v)| (*k, v))
    }
}

struct PlannedObserve {
    center_alt: f64,
    center_az: f64,
    assignments: BTreeMap<String, String>,
    duration_seconds: i64,
    program: String,
}

struct BestField {
    total: f64,
    center_alt: f64,
    center_az: f64,
    chosen: FiberMap<(f64, usize, f64)>, // fiber -> (score, target index, margin)
}

struct FiberInfo {
    target_index: usize,
    alt: f64,
    az: f64,
    model: f64,
    up: f64,
    k: f64,
    band: &'static str,
}

pub struct Planner {
    pub mem: Memory,
    llm: LlmClient,
    trace: TraceLog,

    observe_count: u64,
    reports: u32,
    correct_reports: u32,
    last_report_hours: f64,
    suspicion_hours: Vec<f64>,
    night_index_seen: Option<usize>,
    consecutive_reports: u32,
    total_assigned: i64,
    total_hit: i64,
    /// Current timed-request planning view, refreshed at every `plan`.
    request_view: BTreeMap<usize, RequestEntry>,
    /// Request ids whose registration diagnostic was already logged.
    logged_requests: HashSet<String>,
    /// pointing_offset (Hard-mode hidden pointing bias) probing. hit_count is
    /// a pure geometry verdict: a zero-hit streak (assigned>=3) can only come
    /// from a pointing bias; weather zeroes the score, not the hit.
    zero_hit_streak: u32,
    pointing_correction: (f64, f64),
    probe_list: Vec<(f64, f64)>,
    probe_active: Option<(f64, f64)>,
    probes_done: bool,
}

impl Planner {
    pub fn new(config: &Config, llm: LlmClient) -> Planner {
        let required_count = config.targets.iter().filter(|t| t.required).count();
        log(&format!(
            "planner: {} targets ({} required), {} nights, llm model={} base_url={}",
            config.targets.len(),
            required_count,
            config.nights.len(),
            llm.model(),
            llm.base_url()
        ));
        Planner {
            mem: Memory::new(config),
            llm,
            trace: TraceLog::from_env(),
            observe_count: 0,
            reports: 0,
            correct_reports: 0,
            last_report_hours: f64::NEG_INFINITY,
            suspicion_hours: Vec::new(),
            night_index_seen: None,
            consecutive_reports: 0,
            total_assigned: 0,
            total_hit: 0,
            request_view: BTreeMap::new(),
            logged_requests: HashSet::new(),
            zero_hit_streak: 0,
            pointing_correction: (0.0, 0.0),
            probe_list: Vec::new(),
            probe_active: None,
            probes_done: false,
        }
    }

    // -- top-level decision ----------------------------------------------------

    pub fn decide(&mut self, sequence: i64, snapshot: &DecisionSnapshot, config: &Config) -> DecisionResponse {
        let Some(now) = scoring::parse_utc(&snapshot.now_utc) else {
            log(&format!("planner: unparseable now_utc {:?}, falling back to wait", snapshot.now_utc));
            return crate::validate::safe_fallback(sequence, config, "unparseable now_utc");
        };
        let hours = (now - config.survey_start_unix) / 3600.0;

        self.mem.on_messages(config, &snapshot.new_messages, snapshot.latest_bulletin.as_ref());
        let active_requests: &[Value] = snapshot.active_requests.as_deref().unwrap_or(&[]);
        self.mem.update_requests(config, active_requests, now);
        self.log_new_requests(config, now);
        self.mem.on_result(config, snapshot.last_result.as_ref(), hours);

        if let Some(last_result) = &snapshot.last_result {
            let action = last_result.get("action").and_then(|v| v.as_str()).unwrap_or("");
            if action == "report" && last_result.get("correct").and_then(|v| v.as_bool()).unwrap_or(false) {
                self.correct_reports += 1;
            }
            if action == "observe" {
                let assigned = last_result.get("assigned_count").and_then(|v| v.as_i64()).unwrap_or(0);
                let hit = last_result.get("hit_count").and_then(|v| v.as_i64()).unwrap_or(0);
                self.total_assigned += assigned;
                self.total_hit += hit;
                self.track_pointing(assigned, hit);
            }
        }
        self.pace(config, snapshot, now);

        let Some((night_index, night_start, night_end)) = config.current_night(now) else {
            return match config.next_night_start(now) {
                Some(next) => {
                    let mut response = DecisionResponse::new(sequence, "wait").with_reason("daytime: sleep until the next night");
                    response.until_utc = Some(scoring::format_utc(next));
                    self.validate_or_fallback(response, sequence, config)
                }
                None => DecisionResponse::new(sequence, "finish").with_reason("no observing night left"),
            };
        };

        if self.night_index_seen != Some(night_index) {
            self.night_index_seen = Some(night_index);
            self.night_advice(night_start, snapshot);
        }

        if night_end - now < config.min_duration_seconds as f64 {
            return match config.next_night_start(now) {
                Some(next) => {
                    let mut response = DecisionResponse::new(sequence, "wait").with_reason("night ending");
                    response.until_utc = Some(scoring::format_utc(next));
                    self.validate_or_fallback(response, sequence, config)
                }
                None => DecisionResponse::new(sequence, "finish").with_reason("survey over"),
            };
        }

        if self.mem.site_closed() {
            let mut response = DecisionResponse::new(sequence, "wait").with_reason("bulletin: rain/storm over the whole sky");
            response.duration_seconds = Some(to_next_slot(now, night_start, config.slot_seconds));
            return self.validate_or_fallback(response, sequence, config);
        }

        if let Some(report) = self.maybe_report(sequence, config, hours, snapshot) {
            return self.validate_or_fallback(report, sequence, config);
        }

        let response = match self.plan(config, now, night_end, night_index, hours) {
            Some(plan) => {
                self.observe_count += 1;
                let reason = format!("{} fibres, program {}", plan.assignments.len(), plan.program);
                let mut response = DecisionResponse::new(sequence, "observe").with_reason(reason);
                response.pointing = Some(Pointing { alt_deg: plan.center_alt, az_deg: plan.center_az });
                response.assignments = Some(plan.assignments);
                response.duration_seconds = Some(plan.duration_seconds);
                response.program = Some(plan.program);
                response
            }
            None => {
                let mut response = DecisionResponse::new(sequence, "wait").with_reason("nothing useful is up");
                response.duration_seconds = Some(to_next_slot(now, night_start, config.slot_seconds));
                response
            }
        };
        self.validate_or_fallback(response, sequence, config)
    }

    pub fn on_finish(&mut self, config: &Config, payload: &Value) {
        let mut record = json!({"event": "finish"});
        if let (Some(dst), Some(src)) = (record.as_object_mut(), payload.as_object()) {
            for (k, v) in src {
                dst.insert(k.clone(), v.clone());
            }
        }
        self.trace.write(record);
        self.trace = TraceLog::closed();
        log(&format!(
            "planner: finished termination_reason={} observes={} reports={} llm_calls={}",
            payload.get("termination_reason").and_then(|v| v.as_str()).unwrap_or("None"),
            self.observe_count,
            self.reports,
            self.llm.calls_made
        ));
        // End-of-season diagnostic: dump the still-unfinished required targets
        // (the agent.log comes back with the result bundle) to tell "sky
        // unreachable" apart from "visible but never scheduled".
        let threshold = config.knobs.required_threshold;
        let missing: Vec<usize> = (0..config.targets.len()).filter(|&i| config.targets[i].required && self.mem.factor[i] < threshold).collect();
        log(&format!("planner: required missing estimate {}", missing.len()));
        for &i in missing.iter().take(12) {
            let t = &config.targets[i];
            log(&format!(
                "planner:   miss {} ra={:.1} dec={:.1} hmax={:.0} lastN={} f={:.2} att={}",
                t.target_id, t.ra_deg, t.dec_deg, t.hmax_deg, t.last_night, self.mem.factor[i], self.mem.attempts[i]
            ));
        }
    }

    /// Called by the main loop right after an action is validated, so the
    /// consecutive-report counter (enforced by `validate`) stays correct even
    /// when a fallback replaced it.
    pub fn note_action(&mut self, action: &str) {
        self.consecutive_reports = if action == "report" { self.consecutive_reports + 1 } else { 0 };
    }

    fn validate_or_fallback(&self, response: DecisionResponse, sequence: i64, config: &Config) -> DecisionResponse {
        match crate::validate::validate(&response, config, self.consecutive_reports) {
            Ok(()) => response,
            Err(reason) => {
                log(&format!("planner: built an invalid {} response ({reason}); using the safe fallback", response.action));
                crate::validate::safe_fallback(sequence, config, &reason)
            }
        }
    }

    /// One diagnostic line per request, the first time it is seen: the
    /// per-target visibility facts behind the request bonus.
    fn log_new_requests(&mut self, config: &Config, now: f64) {
        let mut fresh: Vec<(String, f64, i64, Vec<String>)> = Vec::new();
        for (request_id, req) in &self.mem.requests {
            if self.logged_requests.contains(request_id) {
                continue;
            }
            let window_h = (req.deadline_unix - now) / 3600.0;
            let parts = req
                .targets
                .iter()
                .map(|&i| {
                    let t = &config.targets[i];
                    format!(
                        "{}(ra={:.1},dec={:.1},hmax={:.0},lastN={},f={:.2})",
                        t.target_id, t.ra_deg, t.dec_deg, t.hmax_deg, t.last_night, self.mem.factor[i]
                    )
                })
                .collect();
            fresh.push((request_id.clone(), window_h, req.minimum, parts));
        }
        for (request_id, window_h, minimum, parts) in fresh {
            self.logged_requests.insert(request_id.clone());
            log(&format!("planner: request {request_id} min={minimum} window={window_h:.1}h targets: {}", parts.join(" ")));
        }
    }

    /// Do less work per decision when the wall clock is short for the nights
    /// still to come.
    fn pace(&mut self, config: &Config, snapshot: &DecisionSnapshot, now: f64) {
        let remaining_wall = snapshot.wallclock.remaining_seconds;
        let night_seconds: f64 = scoring::py_sum(config.nights.iter().filter(|n| n.end_unix > now).map(|n| (n.end_unix - n.start_unix.max(now)).max(0.0)));
        let decisions_left = (night_seconds / 700.0).max(1.0);
        let per_decision = remaining_wall / decisions_left;
        let level = if per_decision > 0.12 { 0 } else if per_decision > 0.04 { 1 } else { 2 };
        if level != self.mem.fast_level {
            log(&format!("planner: pace level {} ({:.0} ms per decision left)", level, per_decision * 1000.0));
            self.mem.fast_level = level;
        }
    }

    // -- pointing_offset probing and compensation -------------------------------

    fn track_pointing(&mut self, assigned: i64, hit: i64) {
        if let Some(probe) = self.probe_active {
            if assigned >= 3 && hit >= std::cmp::max(2, assigned / 2) {
                self.pointing_correction = probe;
                self.probe_list.clear();
                self.probes_done = true;
                log(&format!("planner: pointing offset compensation locked {:?}", self.pointing_correction));
            }
            self.probe_active = None;
            if self.probe_list.is_empty() && self.pointing_correction == (0.0, 0.0) {
                // Every probe missed: not (or not measurable) a pointing bias;
                // give up rather than burn exposures.
                self.probes_done = true;
            }
        }
        if assigned >= 3 && hit == 0 {
            self.zero_hit_streak += 1;
        } else {
            self.zero_hit_streak = 0;
        }
        if self.zero_hit_streak >= 3
            && !self.probes_done
            && self.pointing_correction == (0.0, 0.0)
            && self.probe_active.is_none()
            && self.probe_list.is_empty()
        {
            self.probe_list =
                vec![(0.4, 0.0), (-0.4, 0.0), (0.0, 0.4), (0.0, -0.4), (0.8, 0.0), (-0.8, 0.0), (0.0, 0.8), (0.0, -0.8)];
            log("planner: zero-hit streak x3, probing for hidden pointing offset");
        }
    }

    /// Pointing-bias compensation: the backend adds a fixed offset to the
    /// pointing we submit, so the compensation applies its inverse. In probe
    /// mode one exposure tries one candidate offset at a time.
    fn corrected_pointing(&mut self, alt: f64, az: f64) -> (f64, f64) {
        let mut correction = self.pointing_correction;
        if self.probe_active.is_none() && !self.probe_list.is_empty() {
            self.probe_active = Some(self.probe_list.remove(0));
        }
        if let Some(probe) = self.probe_active {
            correction = probe;
        }
        if correction == (0.0, 0.0) {
            return (alt, az);
        }
        let alt = (alt + correction.0).clamp(0.0, 90.0);
        let az = (az + correction.1).rem_euclid(360.0);
        (round4(alt), round4(az))
    }

    // -- LLM: two calls once per night, logged but never applied -----------------

    fn night_advice(&mut self, night_start: f64, snapshot: &DecisionSnapshot) {
        // The night label is the civil date 12h before night start (the
        // evening the night begins on), matching the forecast notices'
        // "nights" lists.
        let night_date = scoring::format_utc(night_start - 12.0 * 3600.0)[..10].to_string();
        let left = snapshot.wallclock.remaining_seconds;

        let forecast_tonight: Vec<Value> = self
            .mem
            .last_forecast_notices
            .as_array()
            .map(|a| {
                a.iter()
                    .filter(|n| {
                        n.get("nights").and_then(|v| v.as_array()).map(|nights| nights.iter().any(|d| d.as_str() == Some(night_date.as_str()))).unwrap_or(false)
                    })
                    .cloned()
                    .collect()
            })
            .unwrap_or_default();
        let bulletin_notices: Vec<Value> = snapshot
            .latest_bulletin
            .as_ref()
            .and_then(|b| b.get("notices"))
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default();

        let answer_forecast = self.llm.ask_json(
            FORECAST_SYSTEM_PROMPT,
            &json!({
                "night": night_date,
                "forecast_notices_for_tonight": forecast_tonight,
                "current_bulletin_notices": bulletin_notices,
            }),
            left,
        );

        let hit_rate = if self.total_assigned > 0 { self.total_hit as f64 / self.total_assigned as f64 } else { 1.0 };
        let answer_bulletin = self.llm.ask_json(
            BULLETIN_SYSTEM_PROMPT,
            &json!({
                "night": night_date,
                "bulletin_text": bulletin_text(&bulletin_notices),
                "hit_rate_so_far": scoring::round3(hit_rate),
            }),
            left,
        );

        let mut avoid: BTreeSet<String> = BTreeSet::new();
        let mut scales: Vec<f64> = Vec::new();
        for answer in [&answer_forecast, &answer_bulletin] {
            let Some(answer) = answer else { continue };
            for d in answer.get("avoid_directions").and_then(|v| v.as_array()).into_iter().flatten() {
                if let Some(d) = d.as_str() {
                    let d = d.to_uppercase();
                    if scoring::direction_azimuth(&d).is_some() {
                        avoid.insert(d);
                    }
                }
            }
            // Duration advice is recorded, never adopted (pinned 1.0): a ±2%
            // nightly wobble measurably broke the exposure chain on the delta
            // card (required misses 5->15, report chain broken). The call, the
            // parsing, the validation and the tracing all stay (judging
            // criterion); the actual exposure tuning is left to the measured
            // scale learning.
            let parseable = match answer.get("duration_scale") {
                None => true, // Python: answer.get("duration_scale", 1.0) defaults to 1.0
                Some(v) => v.as_f64().is_some() || v.as_str().map(|s| s.parse::<f64>().is_ok()).unwrap_or(false),
            };
            if parseable {
                scales.push(1.0);
            }
        }
        if avoid.len() >= 7 {
            // Avoiding seven of eight directions equals a full stop -- bad
            // advice (seen in the cloud: avoid=8). Real full-sky closures are
            // handled by site_closed(), not by advice.
            log(&format!("planner: discarding blanket avoid advice {}", py_list(&avoid)));
            avoid.clear();
        }
        // Keep only directions that actually appear in tonight's
        // forecast/bulletin: weak models invent avoidance out of thin air
        // (seen in the cloud: avoid=['N','W'] with zero notices that night).
        let mut evidence_dirs: BTreeSet<String> = BTreeSet::new();
        for notice in forecast_tonight.iter().chain(bulletin_notices.iter()) {
            if let Some(d) = notice.get("direction").and_then(|v| v.as_str()) {
                let d = d.to_uppercase();
                if scoring::direction_azimuth(&d).is_some() {
                    evidence_dirs.insert(d);
                }
            }
        }
        let dropped: BTreeSet<String> = avoid.difference(&evidence_dirs).cloned().collect();
        if !dropped.is_empty() {
            log(&format!("planner: dropping unsupported avoid advice {} (no notice tonight)", py_list(&dropped)));
            avoid = avoid.intersection(&evidence_dirs).cloned().collect();
        }
        // The avoidance advice is likewise recorded, never applied: even
        // evidence-backed, directionally-correct advice displaced the sequence
        // and broke the fault evidence chain on the delta card (required
        // misses 5->15, real fault unreported, -1300 on one card).
        self.mem.extra_avoid.clear();
        self.mem.duration_scale = if scales.is_empty() { 1.0 } else { scoring::py_sum(scales.iter().copied()) / scales.len() as f64 };
        log(&format!(
            "planner: night {} llm advice (forecast call: {}, bulletin call: {}) advised avoid={} (logged only) duration x{:.2}",
            night_date,
            if answer_forecast.is_some() { "ok" } else { "fell back" },
            if answer_bulletin.is_some() { "ok" } else { "fell back" },
            py_list(&avoid),
            self.mem.duration_scale
        ));
        self.trace.write(json!({
            "event": "night_advice",
            "night_date": night_date,
            "avoid": avoid.iter().cloned().collect::<Vec<_>>(),
            "scale": self.mem.duration_scale,
            "forecast_call_ok": answer_forecast.is_some(),
            "bulletin_call_ok": answer_bulletin.is_some(),
        }));
    }

    // -- instrument fault reporting (deterministic rules + LLM confirmation) -----

    fn maybe_report(&mut self, sequence: i64, config: &Config, hours: f64, snapshot: &DecisionSnapshot) -> Option<DecisionResponse> {
        self.mem.force_program = None;
        let report_cap = if self.correct_reports == 0 { MAX_REPORTS } else { MAX_REPORTS_CONFIRMED };
        if self.reports >= report_cap || hours - self.last_report_hours < 24.0 {
            return None;
        }
        let threshold = if self.reports == 0 { REPORT_DROP } else { REPORT_DROP - 0.07 };
        let Some(evidence) = self.mem.fault_evidence(config) else {
            self.suspicion_hours.clear();
            return None;
        };
        if evidence.drop >= threshold {
            self.suspicion_hours.clear();
            return None;
        }
        if evidence.dark_checks < 6 {
            // Too few dark controls to trust the median break: run a DARK
            // diagnostic program to gather them (no report yet).
            self.mem.force_program = Some("DARK".to_string());
        } else if (evidence.dark_matched as f64) < 0.5 * evidence.dark_checks as f64 {
            self.suspicion_hours.clear();
            return None;
        }
        if self.suspicion_hours.last().map(|&last| hours - last < REPORT_SPACING_HOURS).unwrap_or(false) {
            return None;
        }
        self.suspicion_hours.push(hours);
        if self.suspicion_hours.len() < REPORT_CONFIRMATIONS {
            return None;
        }
        self.suspicion_hours.clear();
        let left = snapshot.wallclock.remaining_seconds;
        let verdict_answer = self.llm.ask_json(CONFIRM_SYSTEM_PROMPT, &evidence.to_json(), left);
        let verdict = verdict_answer.as_ref().and_then(|a| a.get("report")).and_then(|v| v.as_bool());
        if verdict == Some(false) {
            log(&format!(
                "planner: report vetoed by the model at {} ({})",
                snapshot.now_utc,
                evidence.display()
            ));
            self.last_report_hours = hours;
            return None;
        }
        // Note: do NOT tighten the rules when the LLM is absent -- a false
        // report costs 0 inside the free allowance, while missing a real fault
        // is -100 plus an efficiency loss compounding to season end (measured
        // ~-1400 locally). The report policy stays aggressive; MAX_REPORTS
        // caps the penalty risk.
        self.reports += 1;
        self.last_report_hours = hours;
        self.mem.forget_quality_history();
        log(&format!("planner: reporting instrument fault at {} evidence={}", snapshot.now_utc, evidence.display()));
        let mut response = DecisionResponse::new(sequence, "report")
            .with_reason(format!("quality dropped to {:.0}% of the earlier level", evidence.drop * 100.0));
        response.decision_source = Some(if verdict == Some(true) { "llm-confirmed" } else { "rule" }.to_string());
        Some(response)
    }

    // -- planning value / achievability -----------------------------------------

    /// Planning value of fully completing target `i` from here (ignores how
    /// much exposure is achievable tonight).
    fn value(&self, config: &Config, i: usize) -> f64 {
        let target = &config.targets[i];
        let f = self.mem.factor[i];
        let threshold = config.knobs.required_threshold;
        // Unfinished required targets are exempt from the miss decay (a missed
        // one is -50; sinking it means it never gets scheduled again).
        let damp = if target.required && f < threshold { 1.0 } else { 0.6_f64.powi(self.mem.misses[i] as i32) };
        let mut value = if target.required {
            if f >= threshold {
                target.science_weight * (1.0 - f * f).max(0.0) * damp
            } else {
                (target.science_weight * (1.0 - f * f) + REQUIRED_BONUS * if f < 0.5 { 1.0 } else { 0.35 }) * damp
            }
        } else if f >= DONE_FACTOR {
            0.0
        } else {
            target.science_weight * (1.0 - f * f) * damp
        };
        if let Some(entry) = self.request_view.get(&i) {
            value += entry.bonus;
        }
        value
    }

    /// Expected gain from target `i` if it gets the best exposure tonight can
    /// still give it -- the ranking used both to pick anchors and to score
    /// candidate fibres within one field.
    #[allow(clippy::too_many_arguments)]
    fn achievable(
        &self,
        config: &Config,
        moon: &Moon,
        lst: f64,
        seconds_left: f64,
        night_index: usize,
        altaz_cache: &mut HashMap<usize, (f64, f64)>,
        i: usize,
    ) -> f64 {
        let target = &config.targets[i];
        let (alt, az) = cached_altaz(config, lst, altaz_cache, i);
        let lunar = moon.lunar_factor(target.ra_deg, target.dec_deg);
        let model = scoring::quality_model(alt, lunar, config.q0, config.airmass_exponent);
        let flux0t0 = (config.flux_zero_point * config.exposure_zero_point_seconds).max(1e-9);
        let k = target.feature_flux * model * self.mem.scale * PLAN_FACTOR_SAFETY / flux0t0;
        let up = if target.hmax_deg < 180.0 {
            (target.hmax_deg - scoring::wrap180(lst - target.ra_deg)) / SIDEREAL_DEG_PER_SECOND
        } else {
            1e9
        };
        let reach = (k * (config.max_duration_seconds as f64).min(up).min(seconds_left)).min(1.0);
        let f = self.mem.factor[i];
        let mut gain = target.science_weight * (reach * reach - f * f).max(0.0);
        let required_urgent = target.required && f < config.knobs.required_threshold;
        if required_urgent && reach >= config.knobs.required_threshold {
            gain += REQUIRED_BONUS;
        }
        let damp = if required_urgent {
            1.0 // required not yet safe: exempt from attempts/misses decay
        } else {
            0.6_f64.powi(self.mem.misses[i] as i32) * 0.7_f64.powi(self.mem.attempts[i] as i32)
        };
        // Unfinished required targets take the LLM advice at only a 20%
        // discount; real closures apply as usual.
        let direction = if required_urgent { self.mem.direction_factor_required(alt, az) } else { self.mem.direction_factor(alt, az) };
        let mut result = gain * damp * direction;
        if let Some(entry) = self.request_view.get(&i) {
            if reach >= entry.threshold {
                // The timed-request bonus only goes to exposures that can
                // actually cross the threshold tonight (the rule: a single
                // exposure above the threshold; multiple short ones do not
                // stack).
                result += entry.bonus * self.mem.direction_factor(alt, az);
            }
        }
        // Short seasons leave no "later": the rescue window widens by season
        // length -- starving required targets (attempts==0) get a two-night
        // floor, tried ones keep one night, so the floor crowd does not grow
        // and crowd out high-yield pointings.
        let floor_margin = if self.mem.attempts[i] == 0 { 2 } else { 1 };
        if required_urgent && target.last_night - night_index as i64 + 1 <= floor_margin && reach >= 0.35 {
            // Last nights, still below threshold, reachable tonight: guarantee
            // a place in the anchor search, betting the real sky beats the
            // estimate.
            result = result.max(URGENT_REQUIRED_FLOOR);
        }
        result
    }

    // -- main planning pass -------------------------------------------------------

    fn plan(&mut self, config: &Config, now: f64, night_end: f64, night_index: usize, hours: f64) -> Option<PlannedObserve> {
        self.mem.update_scale(hours);
        self.request_view = self.mem.request_view(now);
        let lst = scoring::local_sidereal_deg(now, config.longitude_deg);
        let horizon = night_end.min(config.survey_end_unix);
        let seconds_left = horizon - now;
        if seconds_left < config.min_duration_seconds as f64 {
            return None;
        }
        let min_visible = MIN_VISIBLE_SECONDS.min(seconds_left) * SIDEREAL_DEG_PER_SECOND;

        let mut still_active = Vec::new();
        let mut candidates: Vec<(f64, usize)> = Vec::new();
        for &i in &self.mem.active {
            let v = self.value(config, i);
            if v <= 0.0 {
                continue;
            }
            still_active.push(i);
            let target = &config.targets[i];
            let ha = scoring::wrap180(lst - target.ra_deg);
            let h = target.hmax_deg;
            if -h <= ha && ha <= h - min_visible {
                let nights_left = (target.last_night - night_index as i64 + 1).max(1) as f64;
                let setting = if h < 180.0 { 1.0 + 0.5 * (ha / h).max(0.0) } else { 1.0 };
                candidates.push((v * (1.0 + 2.0 / nights_left) * setting, i));
            }
        }
        self.mem.active = still_active;
        if candidates.is_empty() {
            return None;
        }
        candidates.sort_by(|a, b| b.0.total_cmp(&a.0));

        let moon = Moon::at_with_lst(now + 450.0, lst, config.latitude_deg, config.lunar_model);
        let mut altaz_cache: HashMap<usize, (f64, f64)> = HashMap::new();
        let visible: HashSet<usize> = candidates.iter().map(|&(_, i)| i).collect();
        let mut achievable_cache: HashMap<usize, f64> = HashMap::new();

        let mut anchors: Vec<(f64, usize)> = Vec::new();
        for (checked, &(priority, i)) in candidates.iter().enumerate() {
            if checked >= ANCHOR_POOL && anchors.len() >= 3 * ANCHORS {
                break;
            }
            let a = cached_achievable(self, config, &moon, lst, seconds_left, night_index, &mut altaz_cache, &mut achievable_cache, i);
            let weighted = a * priority / self.value(config, i).max(1e-9);
            if weighted > 0.0 {
                anchors.push((weighted, i));
            }
        }
        if anchors.is_empty() {
            return None;
        }
        anchors.sort_by(|a, b| b.0.total_cmp(&a.0));

        let n_anchors = if self.mem.fast_level >= 1 { 1 } else { ANCHORS };
        let fibers: Vec<i64> = if self.mem.fast_level < 2 { (0..config.grid.n_fibers()).collect() } else { vec![5, 6, 9, 10] };
        let mut best: Option<BestField> = None;
        let mut tried = 0usize;
        for &(_, anchor) in &anchors {
            if tried >= n_anchors && best.is_some() {
                break;
            }
            if tried >= n_anchors + 8 {
                break;
            }
            tried += 1;
            let (a_alt, a_az) = cached_altaz(config, lst, &mut altaz_cache, anchor);
            // M19 band-coherent fill: the anchor's band is the field-wide proxy
            // band; fibre candidates are weighted by "same band as the anchor"
            // (x program multiplier / x mismatch). M18 proved alignment at the
            // duration layer; this layer changes the fibre composition itself.
            let anchor_target = &config.targets[anchor];
            let a_lunar = moon.lunar_factor(anchor_target.ra_deg, anchor_target.dec_deg);
            let a_model = scoring::quality_model(a_alt, a_lunar, config.q0, config.airmass_exponent);
            let a_band = scoring::program_band(a_model * self.mem.scale / 0.95, &config.program);
            let near: Vec<usize> = config
                .neighbours(anchor_target.ra_deg, anchor_target.dec_deg, NEIGHBOUR_RADIUS_DEG)
                .into_iter()
                .filter(|j| visible.contains(j))
                .collect();
            // Preserve the neighbours() iteration order: on exact score ties
            // the first-iterated target keeps the fibre, as in the Python dict.
            let near_values: Vec<(usize, f64)> = near
                .into_iter()
                .map(|j| {
                    let v = cached_achievable(self, config, &moon, lst, seconds_left, night_index, &mut altaz_cache, &mut achievable_cache, j);
                    (j, v)
                })
                .collect();

            for &fiber in &fibers {
                let (d_north, d_east) = config.grid.fiber_center(fiber);
                let (c_alt, c_az) = scoring::shift_altaz(a_alt, a_az, -d_north, -d_east);
                if !(config.minimum_altitude_deg + 1.5 <= c_alt && c_alt <= 89.0) {
                    continue;
                }
                let c_alt = round4(c_alt);
                let c_az = round4(c_az).rem_euclid(360.0);
                let mut chosen: FiberMap<(f64, usize, f64)> = FiberMap::new();
                for &(j, v) in &near_values {
                    if v <= 0.0 {
                        continue;
                    }
                    let (alt, az) = cached_altaz(config, lst, &mut altaz_cache, j);
                    let j_target = &config.targets[j];
                    let j_lunar = moon.lunar_factor(j_target.ra_deg, j_target.dec_deg);
                    let j_model = scoring::quality_model(alt, j_lunar, config.q0, config.airmass_exponent);
                    let j_band = scoring::program_band(j_model * self.mem.scale / 0.95, &config.program);
                    let band_mult = if j_band == a_band { config.program.multiplier_for(j_band) } else { config.program.mismatch_multiplier };
                    let Some((off_n, off_e)) = scoring::tangent_offsets(alt, az, c_alt, c_az) else { continue };
                    let (Some(fib), margin) = config.grid.classify(off_n, off_e) else { continue };
                    let score = v * band_mult * if margin >= EDGE_MARGIN_DEG * (1.0 + 1.5 * self.mem.misses[j] as f64) { 1.0 } else { 0.4 };
                    let better = chosen.get(fib).map(|&(existing, _, _)| score > existing).unwrap_or(true);
                    if better {
                        chosen.set(fib, (score, j, margin));
                    }
                }
                if chosen.is_empty() {
                    continue;
                }
                let total: f64 = scoring::py_sum(chosen.iter().map(|(_, &(score, _, _))| score));
                if best.as_ref().map(|b| total > b.total).unwrap_or(true) {
                    best = Some(BestField { total, center_alt: c_alt, center_az: c_az, chosen });
                }
            }
        }
        let best = best?;
        self.finish_plan(config, now, lst, best.center_alt, best.center_az, best.chosen, seconds_left, &moon, &mut altaz_cache, hours, night_index)
    }

    #[allow(clippy::too_many_arguments)]
    fn finish_plan(
        &mut self,
        config: &Config,
        now: f64,
        lst: f64,
        center_alt: f64,
        center_az: f64,
        chosen: FiberMap<(f64, usize, f64)>,
        seconds_left: f64,
        moon: &Moon,
        altaz_cache: &mut HashMap<usize, (f64, f64)>,
        hours: f64,
        night_index: usize,
    ) -> Option<PlannedObserve> {
        let (center_ra, center_dec) = scoring::altaz_to_radec(center_alt, center_az, lst, config.latitude_deg);
        let center_hmax = scoring::max_hour_angle_deg(center_dec, config.latitude_deg, config.minimum_altitude_deg + 0.3);
        let center_ha = scoring::wrap180(lst - center_ra);
        let flux0t0 = (config.flux_zero_point * config.exposure_zero_point_seconds).max(1e-9);

        let mut info: FiberMap<FiberInfo> = FiberMap::new();
        for (fiber, &(_, j, _)) in chosen.iter() {
            let target = &config.targets[j];
            let (alt, az) = cached_altaz(config, lst, altaz_cache, j);
            let lunar = moon.lunar_factor(target.ra_deg, target.dec_deg);
            let model = scoring::quality_model(alt, lunar, config.q0, config.airmass_exponent);
            let up = if target.hmax_deg < 180.0 {
                (target.hmax_deg - scoring::wrap180(lst - target.ra_deg)) / SIDEREAL_DEG_PER_SECOND
            } else {
                1e9
            };
            let k = target.feature_flux * model * self.mem.scale * PLAN_FACTOR_SAFETY / flux0t0;
            info.set(fiber, FiberInfo { target_index: j, alt, az, model, up, k, band: "BACKUP" });
        }
        let center_up = if center_hmax < 180.0 { (center_hmax - center_ha) / SIDEREAL_DEG_PER_SECOND } else { 1e9 };

        // Only exposures fully inside the request window count towards request
        // completion: the duration may not exceed the earliest relevant
        // deadline.
        let mut deadline_cap: Option<f64> = None;
        for (_, item) in info.iter() {
            if let Some(entry) = self.request_view.get(&item.target_index) {
                let cap = entry.deadline_unix - now;
                deadline_cap = Some(match deadline_cap {
                    None => cap,
                    Some(existing) => existing.min(cap),
                });
            }
        }
        if deadline_cap.map(|cap| cap < config.min_duration_seconds as f64).unwrap_or(false) {
            deadline_cap = None;
        }

        // Candidate durations = fixed ladder (x LLM duration scale) + critical
        // exposures (NOT scaled: the physical threshold is not discounted).
        // A critical rung is the exact seconds for each assigned target to
        // cross the required threshold / request threshold / saturation g=1,
        // rounded up to 30 s.
        let mut durations: BTreeSet<i64> = BTreeSet::new();
        for base in DURATIONS {
            let d = ((base * self.mem.duration_scale) / 30.0).round_ties_even() * 30.0;
            durations.insert((d as i64).clamp(config.min_duration_seconds, config.max_duration_seconds));
        }
        for (_, item) in info.iter() {
            if item.k <= 0.0 {
                continue;
            }
            let j = item.target_index;
            let mut critical = vec![1.0];
            if config.targets[j].required && self.mem.factor[j] < config.knobs.required_threshold {
                // The required verdict also takes the single-exposure max g
                // WITH quality: a borderline exposure does not count in
                // ordinary weather (same physics as requests), so it gets the
                // same quality margin.
                critical.push((config.knobs.required_threshold + 0.25).min(1.0));
            }
            if let Some(entry) = self.request_view.get(&j) {
                // Request completion is judged on g WITH measured quality
                // (factor x quality, single exposure >= threshold inside the
                // window). Aiming at the bare threshold fails in ordinary
                // weather (measured: 4 borderline targets all lost, -100), so
                // add the 0.25 quality margin.
                critical.push((entry.threshold + 0.25).min(1.0));
            }
            for g in critical {
                let d = (g / item.k / 30.0).ceil() * 30.0;
                durations.insert((d as i64).clamp(config.min_duration_seconds, config.max_duration_seconds));
            }
        }

        // M18 band coherence: the scorer multiplies score x prog_mult (a
        // mismatch only gets x1.0). Let the duration/fibre competition see the
        // value of "the target's band matches this field's best program".
        let band_scale = self.mem.scale / 0.95;
        let mut infos: Vec<(i64, FiberInfo)> = Vec::new();
        for (fiber, item) in info.iter() {
            let mut item = FiberInfo { target_index: item.target_index, alt: item.alt, az: item.az, model: item.model, up: item.up, k: item.k, band: "BACKUP" };
            item.band = scoring::program_band(item.model * band_scale, &config.program);
            infos.push((fiber, item));
        }
        let band_index = |name: &str| match name {
            "DARK" => 0,
            "BRIGHT" => 1,
            _ => 2,
        };
        let mut ref_votes = [0.0f64; 3];
        for (_, item) in &infos {
            ref_votes[band_index(item.band)] += config.targets[item.target_index].science_weight;
        }
        let total_v = scoring::py_sum(ref_votes);
        let mut prog_star = "BACKUP";
        let mut prog_best = f64::NEG_INFINITY;
        for name in ["DARK", "BRIGHT", "BACKUP"] {
            let mine = ref_votes[band_index(name)];
            let v = mine * config.program.multiplier_for(name) + (total_v - mine) * config.program.mismatch_multiplier;
            if v > prog_best {
                prog_best = v;
                prog_star = name;
            }
        }

        let mut best: Option<(f64, i64)> = None;
        for &duration in &durations {
            if duration as f64 > seconds_left || duration as f64 > center_up {
                continue;
            }
            if deadline_cap.map(|cap| duration as f64 > cap).unwrap_or(false) {
                continue;
            }
            let mut gain = 0.0;
            for (_, item) in &infos {
                if item.up < duration as f64 {
                    continue;
                }
                let reached = (item.k * duration as f64).min(1.0);
                let f = self.mem.factor[item.target_index];
                let mult = if item.band == prog_star { config.program.multiplier_for(item.band) } else { config.program.mismatch_multiplier };
                gain += config.targets[item.target_index].science_weight * (reached * reached - f * f).max(0.0) * mult;
                if config.targets[item.target_index].required && f < config.knobs.required_threshold && reached >= 0.5 {
                    gain += REQUIRED_BONUS;
                }
                if let Some(entry) = self.request_view.get(&item.target_index) {
                    if reached >= entry.threshold {
                        gain += entry.bonus;
                    }
                }
            }
            let rate = gain / duration as f64;
            if best.map(|(r, _)| rate > r).unwrap_or(true) {
                best = Some((rate, duration));
            }
        }
        let (rate, mut duration) = best?;
        if rate <= 0.0 {
            if self.mem.has_recent_sample(hours) {
                return None; // the estimate is fresh and says nothing improves here
            }
            // The sky estimate is stale: take one normal exposure to measure
            // it again.
            let fallback = [900i64, 600, 300].into_iter().find(|&d| {
                d as f64 <= seconds_left && d as f64 <= center_up && deadline_cap.map(|cap| d as f64 <= cap).unwrap_or(true)
            })?;
            duration = fallback;
        }

        let mut assignments: BTreeMap<String, String> = BTreeMap::new();
        for (fiber, item) in &infos {
            if item.up >= duration as f64 {
                assignments.insert(fiber.to_string(), config.targets[item.target_index].target_id.clone());
            }
        }
        if assignments.is_empty() {
            return None;
        }

        let mut votes = [0.0f64; 3];
        for (fiber, item) in &infos {
            if !assignments.contains_key(&fiber.to_string()) {
                continue;
            }
            let band = scoring::program_band(item.model * band_scale, &config.program);
            let bonus = if config.targets[item.target_index].required { REQUIRED_BONUS * 0.02 } else { 0.0 };
            votes[band_index(band)] += config.targets[item.target_index].science_weight * (item.k * duration as f64).min(1.0) + bonus;
        }
        let total_votes = votes[0] + votes[1] + votes[2];
        let mut program = "BACKUP".to_string();
        let mut best_score = f64::NEG_INFINITY;
        for name in ["DARK", "BRIGHT", "BACKUP"] {
            let mine = votes[band_index(name)];
            let score = mine * config.program.multiplier_for(name) + (total_votes - mine) * config.program.mismatch_multiplier;
            if score > best_score {
                best_score = score;
                program = name.to_string();
            }
        }
        if let Some(forced) = self.mem.force_program.clone() {
            program = forced;
        }

        let clean = !self.mem.all_sky_notice();
        self.mem.pending.clear();
        for (fiber, item) in &infos {
            if assignments.contains_key(&fiber.to_string()) {
                let prediction_clean = clean && self.mem.direction_factor(item.alt, item.az) >= 1.0;
                self.mem.pending.push((
                    config.targets[item.target_index].target_id.clone(),
                    PendingPrediction { model: item.model, band_model: item.model / 0.95, alt: item.alt, az: item.az, clean: prediction_clean },
                ));
            }
        }
        self.mem.pending_program = program.clone();
        self.mem.pending_duration = duration as f64;
        self.mem.pending_night = night_index as i64;

        let (out_alt, out_az) = self.corrected_pointing(center_alt, center_az);
        Some(PlannedObserve { center_alt: out_alt, center_az: out_az, assignments, duration_seconds: duration, program })
    }
}

fn to_next_slot(now: f64, night_start: f64, slot_seconds: f64) -> i64 {
    let slot = slot_seconds.max(1.0);
    let into = (now - night_start).rem_euclid(slot);
    let wait = if into == 0.0 { slot } else { slot - into };
    wait.clamp(60.0, 3600.0) as i64 // `as` truncates, like Python's int()
}

fn cached_altaz(config: &Config, lst: f64, cache: &mut HashMap<usize, (f64, f64)>, i: usize) -> (f64, f64) {
    *cache.entry(i).or_insert_with(|| {
        let t = &config.targets[i];
        scoring::radec_to_altaz(t.ra_deg, t.dec_deg, lst, config.latitude_deg)
    })
}

#[allow(clippy::too_many_arguments)]
fn cached_achievable(
    planner: &Planner,
    config: &Config,
    moon: &Moon,
    lst: f64,
    seconds_left: f64,
    night_index: usize,
    altaz_cache: &mut HashMap<usize, (f64, f64)>,
    achievable_cache: &mut HashMap<usize, f64>,
    i: usize,
) -> f64 {
    if let Some(&v) = achievable_cache.get(&i) {
        return v;
    }
    let v = planner.achievable(config, moon, lst, seconds_left, night_index, altaz_cache, i);
    achievable_cache.insert(i, v);
    v
}

/// Python's `sorted(set)` rendered as a list repr: ['NE', 'W'].
fn py_list(set: &BTreeSet<String>) -> String {
    let items: Vec<String> = set.iter().map(|d| format!("'{d}'")).collect();
    format!("[{}]", items.join(", "))
}

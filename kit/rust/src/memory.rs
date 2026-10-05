//! What the agent remembers between decisions, and the stderr logger.
//!
//! Everything stored here is derived from the agent's *own* run: hit scores
//! the backend reported back (`last_result.hits`), the public bulletins and
//! forecasts in `new_messages`/`latest_bulletin`, and the timed observation
//! requests in `active_requests`. No scenario truth ever passes through this
//! module -- it only ever sees what the protocol already sent the agent.
//!
//! This is the Rust port of the Python agent's `agent_core/state.py`
//! (`SurveyState`'s learned state) plus `agent_core/memory.py` (`TraceLog`);
//! the static config half of `SurveyState` lives in `state::Config`.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::io::Write as _;
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::Value;

use crate::scoring::{self, round3};
use crate::state::Config;

/// Writes one timestamped line to stderr. Per the guide, stdout is reserved
/// for protocol JSON only, so every diagnostic goes here. Never panics: a
/// broken stderr pipe must not take down the agent either.
pub fn log(text: &str) {
    let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default();
    let _ = std::io::stderr().write_fmt(format_args!("[{:.3}] {}\n", now.as_secs_f64(), text));
}

/// How long a quality sample stays "recent enough" to trust over the older
/// `prior_scale` median -- mirrors the Python agent's `SKY_MEMORY_HOURS`.
const SKY_MEMORY_HOURS: f64 = 2.0;
const SAMPLES_CAP: usize = 24;
const ALL_RATIOS_CAP: usize = 400;
/// Fault evidence windows (Python `RECENT_SAMPLES` / `EARLIER_SAMPLES`).
const RECENT_SAMPLES: usize = 60;
const EARLIER_SAMPLES: usize = 60;
const BAND_CHECKS_CAP: usize = 60;
/// Only the most recent assignments this long are checked against a repeat
/// miss at (nearly) the same pointing -- `direction_factor`'s "blocked" list.
const BLOCKED_RECENT: usize = 40;

const BLOCKING_KINDS: [&str; 2] = ["terrain_obstruction", "rocket_launch"];

/// What the planner expected for one assigned fibre, kept only long enough to
/// interpret the matching `last_result` on the next `decision_request`.
pub struct PendingPrediction {
    pub model: f64,
    pub band_model: f64,
    pub alt: f64,
    pub az: f64,
    /// True when no all-sky notice / directional block applied at plan time.
    pub clean: bool,
}

/// One timed observation request (record_type "observation_request"), keyed by
/// request id inside `Memory::requests`. Progress is refreshed from the
/// engine's own `active_requests` snapshot every decision.
pub struct Request {
    pub targets: BTreeSet<usize>,
    pub minimum: i64,
    pub threshold: f64,
    pub reward: f64,
    pub deadline_unix: f64,
    pub completed: BTreeSet<usize>,
}

/// Per-target planning view of the active requests: `(bonus, threshold,
/// deadline_unix)` for targets still short of `minimum_completed`.
#[derive(Clone, Copy)]
pub struct RequestEntry {
    pub bonus: f64,
    pub threshold: f64,
    pub deadline_unix: f64,
}

/// The median-break evidence bundle for the instrument-fault report chain
/// (Python `FaultEvidence` namedtuple, same fields and rounding).
pub struct FaultEvidence {
    pub recent_median: f64,
    pub earlier_median: f64,
    pub drop: f64,
    pub recent_samples: usize,
    pub recent_nights: usize,
    pub earlier_samples: usize,
    pub dark_checks: usize,
    pub dark_matched: usize,
}

impl FaultEvidence {
    pub fn to_json(&self) -> Value {
        serde_json::json!({
            "recent_median": self.recent_median,
            "earlier_median": self.earlier_median,
            "drop": self.drop,
            "recent_samples": self.recent_samples,
            "recent_nights": self.recent_nights,
            "earlier_samples": self.earlier_samples,
            "dark_checks": self.dark_checks,
            "dark_matched": self.dark_matched,
        })
    }

    pub fn display(&self) -> String {
        format!(
            "FaultEvidence(recent_median={}, earlier_median={}, drop={}, recent_samples={}, recent_nights={}, earlier_samples={}, dark_checks={}, dark_matched={})",
            self.recent_median,
            self.earlier_median,
            self.drop,
            self.recent_samples,
            self.recent_nights,
            self.earlier_samples,
            self.dark_checks,
            self.dark_matched
        )
    }
}

/// Everything the agent has learned so far: per-target completion progress,
/// the learned sky-quality scale, public weather/terrain notices, the timed
/// observation requests, and the clean-sky quality history the fault
/// diagnostic runs on.
pub struct Memory {
    pub factor: Vec<f64>,
    pub misses: Vec<u32>,
    pub attempts: Vec<u32>,
    /// Targets still worth considering at all (pruned once truly done; see
    /// `planner::value`). Rebuilt wholesale on a Hard-mode `state_resync`.
    pub active: Vec<usize>,

    pub scale: f64,
    prior_scale: f64,
    samples: VecDeque<(f64, f64)>,
    all_ratios: VecDeque<f64>,
    /// (hours, night_index, ratio) for samples taken under clean sky -- the
    /// series the instrument-fault median break is computed on.
    clean_history: Vec<(f64, i64, f64)>,
    /// (declared program, matched, model) for clean hits whose multiplier
    /// could be identified -- the dark-control half of the fault diagnostic.
    band_checks: VecDeque<(String, bool, f64)>,

    /// Exposure-duration multiplier from the night-advice LLM step. The
    /// Python agent pins this to 1.0 (advice is logged, never applied); the
    /// field stays so the duration ladder reads the same place.
    pub duration_scale: f64,
    /// Pace level from the planner's wall-clock governor (0 = full search,
    /// 1 = single anchor, 2 = single anchor + central fibres only).
    pub fast_level: u32,
    /// Diagnostic mode: when the fault evidence is too thin on dark controls,
    /// force DARK program for the next exposures to gather them.
    pub force_program: Option<String>,
    /// `(az, alt)` of recent assigned-but-scoreless fibres, used to suspect a
    /// hidden pointing offset or obstruction near that direction.
    blocked: Vec<(f64, f64)>,
    /// `(event_kind, direction)` from the latest bulletin (terrain and
    /// earthquake excluded -- see `on_messages`).
    notices: BTreeSet<(String, String)>,
    terrain: BTreeSet<String>,
    /// Extra compass directions the night-advice LLM step suggested avoiding.
    /// The Python agent always resets this to empty (advice logged only).
    pub extra_avoid: BTreeSet<String>,
    /// The latest `forecast` message's notices, for the next night-advice prompt.
    pub last_forecast_notices: Value,

    /// A `Vec`, not a map: `on_result` drains this in insertion order (the
    /// field-fill order from the planner), and that order feeds the `blocked`
    /// list and the sample rings below -- both must reproduce the Python
    /// dict's insertion-order iteration exactly.
    pub pending: Vec<(String, PendingPrediction)>,
    pub pending_program: String,
    pub pending_duration: f64,
    pub pending_night: i64,

    /// Timed observation requests: request_id -> Request.
    pub requests: BTreeMap<String, Request>,
}

impl Memory {
    pub fn new(config: &Config) -> Memory {
        let n = config.targets.len();
        let active = config.targets.iter().enumerate().filter(|(_, t)| t.hmax_deg > 0.0).map(|(i, _)| i).collect();
        Memory {
            factor: vec![0.0; n],
            misses: vec![0; n],
            attempts: vec![0; n],
            active,
            scale: 1.0,
            prior_scale: 1.0,
            samples: VecDeque::new(),
            all_ratios: VecDeque::new(),
            clean_history: Vec::new(),
            band_checks: VecDeque::new(),
            duration_scale: 1.0,
            fast_level: 0,
            force_program: None,
            blocked: Vec::new(),
            notices: BTreeSet::new(),
            terrain: BTreeSet::new(),
            extra_avoid: BTreeSet::new(),
            last_forecast_notices: Value::Array(vec![]),
            pending: Vec::new(),
            pending_program: "BACKUP".to_string(),
            pending_duration: 0.0,
            pending_night: -1,
            requests: BTreeMap::new(),
        }
    }

    // --- public bulletins/forecasts/requests ------------------------------------------------------

    pub fn on_messages(&mut self, config: &Config, new_messages: &[Value], latest_bulletin: Option<&Value>) {
        for message in new_messages {
            let record_type = message.get("record_type").and_then(|v| v.as_str()).unwrap_or("");
            if record_type == "bulletin" && message.get("initial").and_then(|v| v.as_bool()).unwrap_or(false) {
                for notice in message.get("notices").and_then(|v| v.as_array()).into_iter().flatten() {
                    if notice.get("event_kind").and_then(|v| v.as_str()) == Some("terrain_obstruction") {
                        if let Some(dir) = notice.get("direction").and_then(|v| v.as_str()) {
                            self.terrain.insert(dir.to_string());
                        }
                    }
                }
            } else if record_type == "forecast" {
                self.last_forecast_notices = message.get("notices").cloned().unwrap_or(Value::Array(vec![]));
            } else if record_type == "state_resync" {
                let observed_ids: Vec<String> = message
                    .get("observed_target_ids")
                    .and_then(|v| v.as_array())
                    .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
                    .unwrap_or_default();
                let best_scores = message.get("best_scores").cloned().unwrap_or(Value::Array(vec![]));
                let requests = message.get("observation_requests").and_then(|v| v.as_array()).cloned().unwrap_or_default();
                self.resync(config, &observed_ids, &best_scores, &requests);
            } else if record_type == "observation_request" {
                self.register_request(config, message);
            } else if record_type == "observation_request_result" {
                let request_id = value_id(message.get("request_id"));
                if let Some(request) = self.requests.get_mut(&request_id) {
                    request.completed = id_set(config, message.get("completed_target_ids"));
                }
            }
        }
        self.notices.clear();
        if let Some(bulletin) = latest_bulletin {
            for notice in bulletin.get("notices").and_then(|v| v.as_array()).into_iter().flatten() {
                let kind = notice.get("event_kind").and_then(|v| v.as_str()).unwrap_or("");
                // terrain_obstruction goes to `terrain` (permanent); earthquake is only
                // an aftershock warning -- the event itself lasts minutes and is quality
                // neutral, and keeping it here would mark every later sample "unclean"
                // and freeze the fault evidence.
                if kind == "terrain_obstruction" || kind == "earthquake" {
                    continue;
                }
                let direction = notice.get("direction").and_then(|v| v.as_str()).unwrap_or("");
                self.notices.insert((kind.to_string(), direction.to_string()));
            }
        }
    }

    /// Hard-mode `state_resync` (participant guide, Appendix A): the
    /// backend's recomputed best scores replace our own factor estimates, and
    /// the local win/loss history is voided so targets can be replanned.
    fn resync(&mut self, config: &Config, observed_ids: &[String], best_scores: &Value, requests: &[Value]) {
        let mut best: BTreeMap<String, f64> = BTreeMap::new();
        let rows = best_scores.as_array().cloned().unwrap_or_default();
        if rows.first().map(|r| r.is_object()).unwrap_or(false) {
            for row in &rows {
                if let (Some(id), Some(score)) = (row.get("target_id").and_then(|v| v.as_str()), row.get("best_score").and_then(|v| v.as_f64())) {
                    best.insert(id.to_string(), score);
                }
            }
        } else {
            for (id, score) in observed_ids.iter().zip(rows.iter()) {
                if let Some(score) = score.as_f64() {
                    best.insert(id.clone(), score);
                }
            }
        }
        let top = config.program.richest_multiplier();
        for (i, target) in config.targets.iter().enumerate() {
            let score = best.get(target.target_id.as_str()).copied().unwrap_or(0.0);
            self.factor[i] = if score > 0.0 && target.science_weight > 0.0 {
                (score / (target.science_weight * top)).min(1.0)
            } else {
                0.0
            };
        }
        self.active = config.targets.iter().enumerate().filter(|(_, t)| t.hmax_deg > 0.0).map(|(i, _)| i).collect();
        self.misses.iter_mut().for_each(|m| *m = 0);
        self.attempts.iter_mut().for_each(|a| *a = 0);
        self.pending.clear();
        for snapshot in requests {
            let request_id = value_id(snapshot.get("request_id"));
            if !self.requests.contains_key(&request_id) {
                self.register_request(config, snapshot);
            }
            if let Some(request) = self.requests.get_mut(&request_id) {
                request.completed = id_set(config, snapshot.get("completed_target_ids"));
            }
        }
        log(&format!("memory: applied state_resync for {} target(s)", best.len()));
    }

    // --- timed observation requests --------------------------------------------------------------

    fn register_request(&mut self, config: &Config, record: &Value) {
        let Some(request_id) = record.get("request_id") else { return };
        let request_id = value_id(Some(request_id));
        if request_id.is_empty() {
            return;
        }
        let targets = id_set(config, record.get("target_ids"));
        if targets.is_empty() {
            return;
        }
        let (Some(issued), Some(deadline)) = (
            record.get("issued_at_utc").and_then(|v| v.as_str()).and_then(scoring::parse_utc),
            record.get("deadline_utc").and_then(|v| v.as_str()).and_then(scoring::parse_utc),
        ) else {
            return;
        };
        let _ = issued;
        self.requests.insert(
            request_id,
            Request {
                targets,
                minimum: record.get("minimum_completed").and_then(|v| v.as_i64()).unwrap_or(1),
                threshold: record
                    .get("completion_factor_threshold")
                    .and_then(|v| v.as_f64())
                    .unwrap_or(config.knobs.required_threshold),
                reward: record.get("completion_reward").and_then(|v| v.as_f64()).unwrap_or(0.0),
                deadline_unix: deadline,
                completed: BTreeSet::new(),
            },
        );
    }

    /// Each decision: refresh progress from `payload.active_requests` (the
    /// engine's own in-window ledger view) and drop requests past deadline.
    pub fn update_requests(&mut self, config: &Config, active: &[Value], now_unix: f64) {
        for snapshot in active {
            let request_id = value_id(snapshot.get("request_id"));
            if !self.requests.contains_key(&request_id) {
                self.register_request(config, snapshot);
            }
            if let Some(request) = self.requests.get_mut(&request_id) {
                request.completed = id_set(config, snapshot.get("completed_target_ids"));
            }
        }
        let expired: Vec<String> = self
            .requests
            .iter()
            .filter(|(_, req)| now_unix >= req.deadline_unix)
            .map(|(id, _)| id.clone())
            .collect();
        for request_id in expired {
            self.requests.remove(&request_id);
        }
    }

    /// target index -> (bonus, threshold, deadline) for active requests still
    /// short of `minimum_completed`; bonus ~= reward/minimum, scaled up as the
    /// window tightens.
    pub fn request_view(&self, now_unix: f64) -> BTreeMap<usize, RequestEntry> {
        let mut view: BTreeMap<usize, RequestEntry> = BTreeMap::new();
        for req in self.requests.values() {
            let remaining = req.minimum - req.completed.len() as i64;
            let window = req.deadline_unix - now_unix;
            if remaining <= 0 || window <= 0.0 {
                continue;
            }
            let slack = window / (remaining as f64 * 900.0).max(1.0);
            let bonus = (req.reward / req.minimum.max(1) as f64) * (4.0 + 12.0 / slack.max(1.0)).min(10.0);
            for &i in req.targets.difference(&req.completed) {
                view.entry(i)
                    .and_modify(|e| {
                        e.bonus = e.bonus.max(bonus);
                        e.deadline_unix = e.deadline_unix.min(req.deadline_unix);
                    })
                    .or_insert(RequestEntry { bonus, threshold: req.threshold, deadline_unix: req.deadline_unix });
            }
        }
        view
    }

    pub fn site_closed(&self) -> bool {
        self.notices.iter().any(|(kind, dir)| matches!(kind.as_str(), "rain" | "storm") && dir == "ALL")
    }

    pub fn all_sky_notice(&self) -> bool {
        self.notices.iter().any(|(_, dir)| dir == "ALL")
    }

    /// Public weather/terrain/LLM-advised directions to avoid, plus a short
    /// memory of recent assigned-but-scoreless pointings -- all PUBLIC, all
    /// derived from the agent's own run. 0.0 = don't even try; <1.0 = discount.
    pub fn direction_factor(&self, alt: f64, az: f64) -> f64 {
        self.direction_factor_full(alt, az, true)
    }

    pub fn direction_factor_full(&self, alt: f64, az: f64, include_advice: bool) -> f64 {
        for direction in &self.terrain {
            if let Some(dir_az) = scoring::direction_azimuth(direction) {
                if alt < 50.0 && scoring::az_distance(az, dir_az) <= 60.0 {
                    return 0.0;
                }
            }
        }
        let mut factor: f64 = 1.0;
        for (kind, direction) in &self.notices {
            let Some(dir_az) = scoring::direction_azimuth(direction) else { continue };
            let near = scoring::az_distance(az, dir_az) <= 67.5;
            if BLOCKING_KINDS.contains(&kind.as_str()) && near && alt < 62.0 {
                return 0.0;
            }
            if near && alt < 75.0 {
                factor = factor.min(0.35);
            }
        }
        if include_advice {
            for direction in &self.extra_avoid {
                // LLM avoidance advice is only a mild discount (0.85), never the
                // 0.35 hard cut: weaker models pick a wrong direction one night
                // in three; real closures are guaranteed by bulletin/terrain/
                // measured blockage. (The advice is currently never applied --
                // see the night-advice path -- but the discount stays faithful.)
                if let Some(dir_az) = scoring::direction_azimuth(direction) {
                    if scoring::az_distance(az, dir_az) <= 67.5 && alt < 70.0 {
                        factor = factor.min(0.85);
                    }
                }
            }
        }
        let recent_start = self.blocked.len().saturating_sub(BLOCKED_RECENT);
        for &(blocked_az, blocked_alt) in &self.blocked[recent_start..] {
            if scoring::az_distance(az, blocked_az) <= 12.0 && alt <= blocked_alt + 3.0 {
                factor = factor.min(0.2);
            }
        }
        factor
    }

    /// Direction factor for unfinished required targets: real closures
    /// (terrain/bulletin/measured blockage) apply as usual, LLM advice only
    /// discounts 20% -- advice can be wrong, a missed required is a real -50.
    pub fn direction_factor_required(&self, alt: f64, az: f64) -> f64 {
        self.direction_factor_full(alt, az, true).max(self.direction_factor_full(alt, az, false) * 0.8)
    }

    // --- learning from our own last_result --------------------------------------------------------

    /// Ports the Python agent's `SurveyState.on_result`: backs out each hit's
    /// true completion factor from the declared-vs-mismatch multiplier,
    /// updates per-target misses/attempts, feeds the learned sky-quality
    /// scale, and records the clean-sky history the fault diagnostic reads.
    pub fn on_result(&mut self, config: &Config, last_result: Option<&Value>, hours: f64) {
        let Some(result) = last_result else {
            self.pending.clear();
            return;
        };
        let action = result.get("action").and_then(|v| v.as_str()).unwrap_or("");
        if action != "observe" || self.pending.is_empty() {
            self.pending.clear();
            return;
        }
        let hits: BTreeMap<String, f64> = result
            .get("hits")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|h| Some((h.get("target_id")?.as_str()?.to_string(), h.get("score")?.as_f64()?)))
                    .collect()
            })
            .unwrap_or_default();
        let any_positive = hits.values().any(|&s| s > 0.0);
        let declared = config.program.multiplier_for(&self.pending_program);
        let mismatch = config.program.mismatch_multiplier;
        let flux0t0 = (config.flux_zero_point * config.exposure_zero_point_seconds).max(1e-9);
        let pending_duration = self.pending_duration;
        let pending_program = self.pending_program.clone();
        let pending_night = self.pending_night;

        let pending = std::mem::take(&mut self.pending);
        for (target_id, prediction) in pending {
            let Some(i) = config.index_of(&target_id) else { continue };
            match hits.get(&target_id) {
                None => self.misses[i] += 1,
                Some(&score) if score <= 0.0 => {
                    if any_positive {
                        self.blocked.push((prediction.az, prediction.alt));
                    }
                }
                Some(&score) => {
                    let weight = if config.targets[i].science_weight > 0.0 { config.targets[i].science_weight } else { 1e-9 };
                    let multiplier_seen = score / weight;
                    if prediction.clean {
                        if (multiplier_seen - declared).abs() < 2e-4 {
                            push_capped(&mut self.band_checks, (pending_program.clone(), true, prediction.model), BAND_CHECKS_CAP);
                        } else if (multiplier_seen - mismatch).abs() < 2e-4 {
                            push_capped(&mut self.band_checks, (pending_program.clone(), false, prediction.model), BAND_CHECKS_CAP);
                        }
                    }
                    let factor_if_match = if declared > 0.0 { score / (weight * declared) } else { 0.0 };
                    let factor_if_miss = if mismatch > 0.0 { score / (weight * mismatch) } else { 0.0 };
                    let ratio_match = if config.targets[i].feature_flux > 0.0 && pending_duration > 0.0 && prediction.model > 0.0 {
                        (factor_if_match * flux0t0) / (config.targets[i].feature_flux * pending_duration * prediction.model)
                    } else {
                        0.0
                    };
                    let band = scoring::program_band(ratio_match * prediction.band_model, &config.program);
                    let matched = band == pending_program.as_str();
                    let estimate = if matched { factor_if_match } else { factor_if_miss };
                    let factor = if config.targets[i].required {
                        // Conservative bookkeeping for required targets: when the
                        // multiplier is uncertain, assume the match (larger divisor,
                        // smaller factor) so a required target is never overestimated
                        // into never being re-observed.
                        factor_if_match.min(factor_if_miss)
                    } else {
                        estimate
                    };
                    self.factor[i] = self.factor[i].max(factor.min(1.0));
                    if config.targets[i].required {
                        if self.factor[i] < config.knobs.required_threshold {
                            self.attempts[i] += 1;
                        } else {
                            // Already safe: the failure counter must not drag down
                            // later push-higher scoring.
                            self.attempts[i] = 0;
                        }
                    }
                    // Sky-quality samples always use the maximum-likelihood
                    // estimate; the conservative bookkeeping must not pollute
                    // the scale or the fault statistics.
                    if estimate < 0.97 && config.targets[i].feature_flux > 0.0 && pending_duration > 0.0 && prediction.model > 0.0 {
                        let ratio = (estimate * flux0t0) / (config.targets[i].feature_flux * pending_duration * prediction.model);
                        push_capped(&mut self.samples, (hours, ratio), SAMPLES_CAP);
                        push_capped(&mut self.all_ratios, ratio, ALL_RATIOS_CAP);
                        if prediction.clean {
                            self.clean_history.push((hours, pending_night, ratio));
                        }
                    }
                }
            }
        }
        self.update_scale(hours);
    }

    pub fn has_recent_sample(&self, hours: f64) -> bool {
        self.samples.iter().any(|(when, _)| *when >= hours - SKY_MEMORY_HOURS)
    }

    pub fn update_scale(&mut self, hours: f64) {
        if self.all_ratios.len() >= 8 {
            let mut ordered: Vec<f64> = self.all_ratios.iter().copied().collect();
            ordered.sort_by(|a, b| a.total_cmp(b));
            self.prior_scale = ordered[ordered.len() / 2];
        }
        let mut recent: Vec<f64> = self.samples.iter().filter(|(when, _)| *when >= hours - SKY_MEMORY_HOURS).map(|(_, r)| *r).collect();
        recent.sort_by(|a, b| a.total_cmp(b));
        self.scale = if recent.len() >= 4 { recent[recent.len() / 2].max(0.05) } else { self.prior_scale };
    }

    // --- instrument-fault diagnostics -------------------------------------------------------------

    /// The median-break test on the clean-sky quality history: recent window
    /// vs everything before it, plus the dark-source control from identified
    /// program multipliers. `None` while the history is too thin to judge.
    pub fn fault_evidence(&self, config: &Config) -> Option<FaultEvidence> {
        let history = &self.clean_history;
        if history.len() < RECENT_SAMPLES + EARLIER_SAMPLES {
            return None;
        }
        let mut recent: &[(f64, i64, f64)] = &history[history.len() - RECENT_SAMPLES..];
        let widened_storage: Vec<(f64, i64, f64)>;
        if recent[recent.len() - 1].0 - recent[0].0 < 4.0 {
            // Dense sampling makes RECENT_SAMPLES span too little time: widen
            // "recent" to a trailing 6-hour window so the median still
            // compares now vs before.
            let cutoff = history[history.len() - 1].0 - 6.0;
            widened_storage = history.iter().copied().filter(|s| s.0 >= cutoff).collect();
            if widened_storage.len() >= RECENT_SAMPLES {
                recent = &widened_storage;
            }
        }
        let earlier = &history[..history.len() - recent.len()];
        let span = recent[recent.len() - 1].0 - recent[0].0;
        let nights: BTreeSet<i64> = recent.iter().map(|s| s.1).collect();
        if span < 2.0 || nights.is_empty() || earlier.len() < EARLIER_SAMPLES / 2 {
            return None;
        }
        let mut recent_sorted: Vec<f64> = recent.iter().map(|s| s.2).collect();
        recent_sorted.sort_by(|a, b| a.total_cmp(b));
        let mut earlier_sorted: Vec<f64> = earlier.iter().map(|s| s.2).collect();
        earlier_sorted.sort_by(|a, b| a.total_cmp(b));
        let recent_median = recent_sorted[recent_sorted.len() / 2];
        let earlier_median = earlier_sorted[earlier_sorted.len() / 2];
        let dark_line = config.program.band_dark * 1.3;
        let dark: Vec<&(String, bool, f64)> = self
            .band_checks
            .iter()
            .rev()
            .take(16)
            .filter(|c| c.0 == "DARK" && (c.2 * earlier_median) / 0.95 >= dark_line)
            .collect();
        Some(FaultEvidence {
            recent_median: round3(recent_median),
            earlier_median: round3(earlier_median),
            drop: round3(recent_median / earlier_median.max(1e-9)),
            recent_samples: recent.len(),
            recent_nights: nights.len(),
            earlier_samples: earlier.len(),
            dark_checks: dark.len(),
            dark_matched: dark.iter().filter(|c| c.1).count(),
        })
    }

    /// Called after a confirmed/issued fault report: the fault-era quality
    /// history is poison, so the scale re-learns from a healthy instrument.
    pub fn forget_quality_history(&mut self) {
        self.clean_history.clear();
        self.band_checks.clear();
        self.samples.clear();
        self.all_ratios.clear();
        self.prior_scale = 1.0;
    }
}

fn push_capped<T>(buffer: &mut VecDeque<T>, item: T, cap: usize) {
    buffer.push_back(item);
    if buffer.len() > cap {
        buffer.pop_front();
    }
}

/// `str(x)` for a JSON id that is usually a string but may arrive as a number.
fn value_id(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(s)) => s.clone(),
        Some(v) => v.to_string(),
        None => String::new(),
    }
}

/// Set of catalogue indices named by a JSON string array (unknown ids dropped).
fn id_set(config: &Config, value: Option<&Value>) -> BTreeSet<usize> {
    value
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|v| v.as_str()).filter_map(|id| config.index_of(id)).collect())
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Optional JSONL decision trace (Python `agent_core/memory.py` TraceLog).
// ---------------------------------------------------------------------------

/// Best-effort JSONL trace of planning events (night advice, finish summary),
/// written only when `AGENT_TRACE_PATH` is set. Writing never fails the agent.
pub struct TraceLog {
    handle: Option<std::fs::File>,
}

impl TraceLog {
    pub fn from_env() -> TraceLog {
        let path = std::env::var("AGENT_TRACE_PATH").ok().map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
        let handle = path.and_then(|p| match std::fs::OpenOptions::new().create(true).append(true).open(&p) {
            Ok(f) => Some(f),
            Err(e) => {
                log(&format!("memory: could not open AGENT_TRACE_PATH ({e}); tracing disabled"));
                None
            }
        });
        TraceLog { handle }
    }

    /// A trace that writes nowhere (after `close`, or when tracing is off).
    pub fn closed() -> TraceLog {
        TraceLog { handle: None }
    }

    pub fn write(&mut self, record: Value) {
        let Some(handle) = self.handle.as_mut() else { return };
        let ts = SystemTime::now().duration_since(UNIX_EPOCH).unwrap_or_default().as_secs_f64();
        let mut record = record;
        if let Some(obj) = record.as_object_mut() {
            obj.insert("ts".to_string(), serde_json::json!(ts));
        }
        if let Ok(mut line) = serde_json::to_string(&record) {
            line.push('\n');
            let _ = handle.write_all(line.as_bytes());
            let _ = handle.flush();
        }
    }
}

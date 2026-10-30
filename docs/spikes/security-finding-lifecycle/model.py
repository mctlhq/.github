"""Non-normative reference state machine for a provider-neutral security finding lifecycle.

Reference code for the mctlhq/.github#209 spike, not a service and not the canonical schema.
Transitions are derived from recorded evidence. There is no set-state primitive. Provider
text (description, severity) is stored as untrusted evidence and never branched on.
"""
import hashlib

STRENGTH = ["manual", "provider_claim", "scanner_rerun_exact_revision", "assurance_replay",
            "deterministic_regression_test"]
DISMISS_REASONS = {"false_positive", "accepted_risk", "duplicate", "not_applicable", "provider_withdrawn"}
FAIL_RESULTS = {"error", "unavailable", "timeout", "failed"}
CONFIRM_N = 2


class LifecycleError(ValueError):
    pass


class Finding:
    def __init__(self, key, repo, component, vuln):
        self.id = "sf_" + key[:16]
        self.key, self.repo, self.component, self.vuln = key, repo, component, vuln
        self.state = "open"
        self.observations = []
        self.remediation = None
        self.vulnerable_rev = None
        self.fixed_rev = None
        self.evidence = []          # verification and claim records
        self.regressions = []       # consecutive regression observations
        self.dismissal = None
        self.successors = []
        self.untrusted = []         # provider text, never read by transitions


def canonical_key(vuln, repo, component):
    return hashlib.sha256("\x00".join([vuln, repo, component]).encode()).hexdigest()


class Model:
    def __init__(self, min_strength="scanner_rerun_exact_revision"):
        self.min_strength = min_strength
        self.findings = {}
        self.deliveries = set()

    def _find(self, key):
        return self.findings[key]

    def ingest(self, obs):
        """Add a provider observation. Returns the canonical finding."""
        repo = obs["repository"]["full_name"]
        key = canonical_key(obs["vuln_class_id"], repo, obs["component"])
        dkey = (key, obs["provider"], obs["delivery_id"])  # per finding: a batch delivery carries many
        f = self.findings.get(key)
        if dkey in self.deliveries:
            return f
        self.deliveries.add(dkey)
        if f is None:
            f = self.findings[key] = Finding(key, repo, obs["component"], obs["vuln_class_id"])
        f.observations.append({"provider": obs["provider"], "delivery_id": obs["delivery_id"],
                               "action": obs["action"]})
        f.untrusted.append({"description": obs.get("description"), "severity": obs.get("severity")})
        rev = obs.get("revision") or obs.get("alert", {}).get("most_recent_instance", {}).get("commit_sha")
        action = obs["action"]
        if action in ("fixed",):
            f.evidence.append({"strength": "provider_claim", "rev": rev, "result": "absent"})
        elif f.state == "resolved" and action in ("created", "detected", "reopened", "new_finding", "confirmed"):
            if rev:
                self._regression(f, rev, deterministic=obs["provider"] == "mctl-assurance")
        elif f.state == "dismissed" and action in ("created", "detected", "reopened", "new_finding", "confirmed"):
            f.successors.append({"delivery_id": obs["delivery_id"], "provider": obs["provider"]})
        elif f.vulnerable_rev is None and rev:
            f.vulnerable_rev = rev
        return f

    def _regression(self, f, rev, deterministic):
        # Ancestry is not modelled: any revision other than the vulnerable one is treated as a
        # descendant. A stale rescan of the pre-fix revision must not reopen a resolved finding,
        # and repeat deliveries for one revision count once.
        if rev == f.vulnerable_rev:
            return
        if rev not in f.regressions:
            f.regressions.append(rev)
        if deterministic or len(f.regressions) >= CONFIRM_N:
            f.state = "reopened"
            f.fixed_rev = None
            f.regressions = []

    def triage(self, key, actor):
        f = self._find(key)
        if f.state != "open" or not actor:
            raise LifecycleError("triage requires an open finding and an actor")
        f.state = "triaged"

    def link_remediation(self, key, work_item_ref):
        f = self._find(key)
        if f.state == "dismissed":
            raise LifecycleError("dismissed finding")
        if f.state == "reopened" and f.remediation is not None:
            f.state = "fixing"  # one WorkItem per canonical key: reuse it
            return
        if f.remediation is not None:
            raise LifecycleError("execution_request_open: remediation already linked")
        f.remediation = work_item_ref
        if f.state in ("open", "triaged", "reopened"):
            f.state = "fixing"

    def record_merge(self, key, sha):
        f = self._find(key)
        if f.state != "fixing":
            raise LifecycleError("merge recorded for a finding that is not being fixed")
        f.fixed_rev = sha
        f.state = "awaiting_verification"

    def record_verification(self, key, sha, mechanism, result):
        f = self._find(key)
        if mechanism not in STRENGTH:
            raise LifecycleError("unknown mechanism")
        if f.state not in ("awaiting_verification", "unverifiable"):
            raise LifecycleError("nothing awaits verification")
        if sha != f.fixed_rev:
            raise LifecycleError("verification is not bound to the landed revision")
        f.evidence.append({"strength": mechanism, "rev": sha, "result": result})
        if result in FAIL_RESULTS:
            f.state = "unverifiable"
        elif result == "absent" and STRENGTH.index(mechanism) >= STRENGTH.index(self.min_strength):
            f.state = "resolved"
        elif result == "present":
            f.state = "fixing"
            f.fixed_rev = None
        # a weaker-than-policy result changes nothing

    def dismiss(self, key, actor, authority, reason):
        f = self._find(key)
        if not (actor and authority and reason in DISMISS_REASONS):
            raise LifecycleError("dismissal needs actor, authority and a known reason")
        f.dismissal = {"actor": actor, "authority": authority, "reason": reason}
        f.state = "dismissed"

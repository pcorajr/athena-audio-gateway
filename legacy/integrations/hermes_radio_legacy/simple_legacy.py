"""State-service picture for SimpleController when no Eye is attached (legacy)."""
from integrations.hermes_radio.simple_controller import SimpleController, _num
from . import tools as tools_mod
from .tools import _plain, category, geometry


class LegacySimpleController(SimpleController):
    def _state_service_picture(self):
        """Legacy path: Athena state service (kept for fallback when no eye is configured)."""
        from athena.state_service_client import ClientQueryError

        if not getattr(tools_mod, "_eye_lenient", False):
            strict = tools_mod.usable

            def lenient(frame):
                try:
                    return strict(frame)
                except ClientQueryError as error:
                    if str(error) != "evidence_expired":
                        raise
            tools_mod.usable, tools_mod._eye_lenient = lenient, True
        own = _plain(self.tools.read("ownship", {}).data)["ownship"]
        if own is None:
            return {"available": False, "reason": "Prometheus is not in an aircraft slot (no ownship)"}
        mine = own.get("coalition")
        enemy = "red" if mine == "blue" else "blue"
        cap = min(100, self.tools.config.query_limits.max_rows)
        pic = {"available": True, "ownship": {k: own.get(k) for k in own if k not in ("provenance_key",)}}
        if own.get("position") and own["position"].get("alt_m") is not None:
            pic["ownship"]["altitude_ft_msl"] = _num(own["position"]["alt_m"] / 0.3048)
        for side, coalition in (("hostile", enemy), ("friendly", mine)):
            params = {"coalition": coalition, "state": "present", "limit": cap}
            if own.get("position"):
                params["near"] = {"anchor": "ownship", "radius_m": self.radius_nm * 1852}
            try:
                data = _plain(self.tools.read("query_units", params).data)
            except Exception as error:  # noqa: BLE001 - report, don't hide
                pic[side] = {"error": type(error).__name__ + ": " + str(error)}
                continue
            rows = []
            for row in data.get("rows", []):
                if row.get("unit_id") == own.get("unit_id"):
                    continue
                braa = geometry(own, row)
                rows.append({"type": row.get("unit_type"), "category": category(row.get("unit_type")),
                             "group": row.get("group_name"), "bearing_true": braa["bearing_true_deg"],
                             "range_nm": braa["range_nm"], "alt_ft": braa["altitude_ft_msl"]})
            rows.sort(key=lambda r: (r["range_nm"] is None, r["range_nm"] or 0))
            pic[side] = rows[:25]
        try:
            events = _plain(self.tools.read("recent_events", {"limit": 12}).data)
            pic["recent_events"] = [{k: e.get(k) for k in ("event_type", "observed_at_utc", "text")}
                                    for e in events.get("events", [])]
        except Exception as error:  # noqa: BLE001
            pic["recent_events"] = {"error": str(error)}
        return pic

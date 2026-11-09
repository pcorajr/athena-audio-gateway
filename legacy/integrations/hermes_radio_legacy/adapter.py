from athena.state_service_client import ClientQueryError
from integrations.hermes_radio.contract import response
from .facts import CAPABILITIES, render
from .hermes_client import classify
from .tactical import TACTICAL_INTENTS
class Adapter:
    def __init__(self, classifier, facts):
        self.classifier, self.facts = classifier, facts

    def exchange(self, request):
        intent = classify(request.transcript, self.classifier)
        if intent not in {"current_state", "recent_events"} | TACTICAL_INTENTS:
            return response(request, speech=CAPABILITIES, state="ok")
        try:
            return render(request, intent, self.facts.query(intent))
        except (ClientQueryError, ValueError, TypeError, KeyError, OverflowError):
            speech = ("No SAM data. Athena not connected." if intent == "threats" else
                      "Unable, Athena not connected." if intent == "home_plate" else "Athena not connected.")
            return response(request, speech=speech, state="unavailable")



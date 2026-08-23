"""Routes for the LifeLink Assistant chat widget.

Two small JSON endpoints power the chat widget in base.html:
- GET  /api/chatbot/common-questions  -> quick-reply buttons shown on open
- POST /api/chatbot/ask               -> send a message, get a reply

The assistant runs on a local Ollama text model (see services.call_chatbot_llm),
kept separate from the vision model used for card/report image scanning.
"""

from flask import jsonify, request
from flask_login import current_user

from services import CHATBOT_COMMON_QUESTIONS, CHATBOT_FALLBACK_MESSAGES, call_chatbot_llm


def register_routes(app):

    @app.route("/api/chatbot/common-questions")
    def api_chatbot_common_questions():
        """Static FAQ shortlist shown as tappable buttons when the widget opens."""
        return jsonify({"questions": CHATBOT_COMMON_QUESTIONS})

    @app.route("/api/chatbot/ask", methods=["POST"])
    def api_chatbot_ask():
        data = request.get_json(silent=True) or {}
        message = (data.get("message") or "").strip()
        history = data.get("history") or []

        if not message:
            return jsonify({"error": "Message can't be empty."}), 400
        if len(message) > 800:
            return jsonify({"error": "That message is too long."}), 400
        if not isinstance(history, list):
            history = []

        user_role = current_user.role if current_user.is_authenticated else "guest"
        status, reply = call_chatbot_llm(message, history, user_role)

        if status != "ok":
            fallback = CHATBOT_FALLBACK_MESSAGES.get(
                status, "Sorry, the assistant is unavailable right now."
            )
            return jsonify({"status": status, "reply": fallback})

        return jsonify({"status": "ok", "reply": reply})

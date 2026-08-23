"""Route modules for the LifeLink platform.

Each module exposes ``register_routes(app)`` which decorates the Flask app
instance directly (rather than using named blueprints) so that every endpoint
keeps its original function name — ``url_for("login")``, ``url_for("dashboard")``
etc. continue to work unchanged in code and templates.
"""


def register_all_routes(app):
    from . import public, auth, profile, donor, patient, admin, api, chatbot
    public.register_routes(app)
    auth.register_routes(app)
    profile.register_routes(app)
    donor.register_routes(app)
    patient.register_routes(app)
    admin.register_routes(app)
    api.register_routes(app)
    chatbot.register_routes(app)

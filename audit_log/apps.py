from django.apps import AppConfig


class AuditLogConfig(AppConfig):
    # The other apps of this project all end up with an AutoField, because no
    # default is set in the settings
    default_auto_field = 'django.db.models.AutoField'
    name = 'audit_log'
    verbose_name = 'Audit Log'

    def ready(self):
        # Connected here and not at the top of signals.py, so that a module that
        # only wants to use the models does not start listening to everything
        from . import signals

        signals.connect()

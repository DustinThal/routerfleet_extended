"""The audit trail in the Django admin, read-only.

The admin is not where this project is administered, but it is reachable, and a
registered model can be deleted from there unless it says otherwise. So both of
these say so: no adding, no changing, no deleting - and no actions, because a
read-only changelist would otherwise still offer the collector's delete_selected.

Viewing is left alone, so that an administrator can still look at a row.
"""
from django.contrib import admin

from .models import ChangeRecord, LoginRecord


class AuditAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return [field.name for field in self.model._meta.fields]


@admin.register(LoginRecord)
class LoginRecordAdmin(AuditAdmin):
    list_display = ('created', 'username', 'successful', 'user_level', 'ip', 'actor_kind')
    list_filter = ('successful',)
    search_fields = ('username', 'ip')


@admin.register(ChangeRecord)
class ChangeRecordAdmin(AuditAdmin):
    list_display = ('created', 'action', 'model_label', 'object_repr', 'actor_name', 'ip')
    list_filter = ('action', 'model_label')
    search_fields = ('object_repr', 'actor_name', 'ip')

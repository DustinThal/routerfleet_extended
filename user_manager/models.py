from django.db import models
from django.contrib.auth.models import User
import uuid


class UserAcl(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    user_level = models.PositiveIntegerField(default=0, choices=(
        (10, 'Viewer'),
        (20, 'Backup Operator'),
        (30, 'Host Manager'),
        (40, 'configuration Manager'),
        (50, 'Administrator'),
    ))
    address_link = models.CharField(max_length=10, default='none', choices=(
        ('none', 'No links'),
        ('ssh', 'Open with SSH'),
        ('winbox', 'Open with Winbox'),
        ('both', 'Show a menu (SSH / Winbox)'),
    ), help_text='What a click on the address of a router in the router list does.')

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)
    uuid = models.UUIDField(primary_key=True, editable=False, default=uuid.uuid4)

    def __str__(self):
        return self.user.username

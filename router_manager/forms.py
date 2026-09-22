from django import forms
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Submit, Row, Column, HTML
from backup.models import BackupProfile
from .models import Router, RouterGroup, SSHKey
from routerlib.functions import test_authentication, connect_to_ssh
import ipaddress
import socket


class RouterForm(forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput, required=False)

    class Meta:
        model = Router
        fields = ['name', 'port', 'address', 'username', 'password', 'ssh_key', 'monitoring', 'router_type', 'enabled', 'backup_profile']

    def __init__(self, *args, **kwargs):
        super(RouterForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        if self.instance.pk:
            delete_html = "<a href='javascript:void(0)' class='btn btn-outline-danger' data-command='delete' onclick='openCommandDialog(this)'>Delete</a>"
            if self.instance.password:
                self.fields['password'].widget.attrs['placeholder'] = '************'
        else:
            delete_html = ''
        self.helper.layout = Layout(
            Row(
                Column('name', css_class='form-group col-md-6 mb-0'),
                Column('ssh_key', css_class='form-group col-md-6 mb-0'),
                css_class='form-row'
            ),
            Row(
                Column('username', css_class='form-group col-md-6 mb-0'),
                Column('password', css_class='form-group col-md-6 mb-0'),
                css_class='form-row'
            ),
            Row(
                Column('address', css_class='form-group col-md-6 mb-0'),
                Column('port', css_class='form-group col-md-6 mb-0'),
                css_class='form-row'
            ),

            'backup_profile',
            'router_type',
            'monitoring',
            'enabled',
            Row(
                Column(
                    Submit('submit', 'Save', css_class='btn btn-success'),
                    HTML(' <a class="btn btn-secondary" href="/router/list/">Back</a> '),
                    HTML(delete_html),
                    css_class='col-md-12'),
                css_class='form-row'
            )
        )

    def clean(self):
        cleaned_data = super().clean()
        name = cleaned_data.get('name')
        ssh_key = cleaned_data.get('ssh_key')
        username = cleaned_data.get('username')
        password = cleaned_data.get('password')
        address = cleaned_data.get('address')
        router_type = cleaned_data.get('router_type')
        backup_profile = cleaned_data.get('backup_profile')
        port = cleaned_data.get('port')

        if name:
            name = name.strip()
            cleaned_data['name'] = name

        if address:
            address = address.lower()
            cleaned_data['address'] = address

            try:
                socket.gethostbyname(address)
            except socket.gaierror:
                try:
                    ipaddress.ip_address(address)
                except ValueError:
                    raise forms.ValidationError('The address field must be a valid hostname or IP address.')

        if router_type == 'monitoring':
            cleaned_data['password'] = ''
            cleaned_data['ssh_key'] = None
            if backup_profile:
                raise forms.ValidationError('Monitoring only routers cannot have a backup profile')
            return cleaned_data
        else:
            if not port:
                raise forms.ValidationError('You must provide a port')
            if not 1 <= port <= 65535:
                raise forms.ValidationError('Invalid port number')

        if ssh_key and password:
            raise forms.ValidationError('You must provide a password or an SSH Key, not both')
        if not ssh_key and not password and not self.instance.password:
            raise forms.ValidationError('You must provide a password or an SSH Key')

        if not password and self.instance.password:
            cleaned_data['password'] = self.instance.password

        if ssh_key and not password:
            cleaned_data['password'] = ''

        test_authentication_success, test_authentication_message = test_authentication(
            router_type, cleaned_data['address'], port, username, cleaned_data['password'], ssh_key
        )
        if not test_authentication_success:
            if test_authentication_message:
                raise forms.ValidationError('Could not authenticate: ' + test_authentication_message)
            else:
                raise forms.ValidationError('Could not authenticate to the router. Please check the credentials and try again.')
        return cleaned_data


class RouterBulkEditForm(forms.Form):
    # Every field is optional: a blank field means "leave this router's value unchanged"
    port = forms.IntegerField(required=False, min_value=1, max_value=65535)
    username = forms.CharField(required=False, max_length=100)
    password = forms.CharField(required=False, widget=forms.PasswordInput)
    ssh_key = forms.ChoiceField(required=False)
    backup_profile = forms.ChoiceField(required=False)
    monitoring = forms.ChoiceField(required=False, choices=[('', 'Unchanged'), ('true', 'Enable'), ('false', 'Disable')])
    enabled = forms.ChoiceField(required=False, choices=[('', 'Unchanged'), ('true', 'Enable'), ('false', 'Disable')])
    internal_notes = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 4, 'cols': 40}))

    def __init__(self, *args, **kwargs):
        super(RouterBulkEditForm, self).__init__(*args, **kwargs)
        self.fields['ssh_key'].choices = (
            [('', 'Unchanged'), ('clear', 'Clear SSH key')]
            + [(str(ssh_key.uuid), ssh_key.name) for ssh_key in SSHKey.objects.all().order_by('name')]
        )
        self.fields['backup_profile'].choices = (
            [('', 'Unchanged'), ('clear', 'Clear backup profile')]
            + [(str(backup_profile.uuid), backup_profile.name) for backup_profile in BackupProfile.objects.all().order_by('name')]
        )
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column('username', css_class='form-group col-md-6 mb-0'),
                Column('port', css_class='form-group col-md-6 mb-0'),
                css_class='form-row'
            ),
            Row(
                Column('password', css_class='form-group col-md-6 mb-0'),
                Column('ssh_key', css_class='form-group col-md-6 mb-0'),
                css_class='form-row'
            ),
            Row(
                Column('monitoring', css_class='form-group col-md-6 mb-0'),
                Column('enabled', css_class='form-group col-md-6 mb-0'),
                css_class='form-row'
            ),
            'backup_profile',
            'internal_notes',
            Row(
                Column(
                    Submit('submit', 'Apply Changes', css_class='btn btn-success'),
                    HTML(' <a class="btn btn-secondary" href="/router/list/">Cancel</a> '),
                    css_class='col-md-12'),
                css_class='form-row'
            )
        )

    def clean(self):
        cleaned_data = super().clean()
        username = cleaned_data.get('username')
        if username:
            username = username.strip()
            cleaned_data['username'] = username

        updatable_fields = ['port', 'username', 'password', 'ssh_key', 'backup_profile', 'monitoring', 'enabled', 'internal_notes']
        if not any(cleaned_data.get(field) for field in updatable_fields):
            raise forms.ValidationError('You must provide at least one field to update')

        ssh_key = cleaned_data.get('ssh_key')
        if cleaned_data.get('password') and ssh_key and ssh_key != 'clear':
            raise forms.ValidationError('You must provide a password or an SSH Key, not both')
        if ssh_key == 'clear' and not cleaned_data.get('password'):
            raise forms.ValidationError('Clearing the SSH key without setting a password would leave the routers without authentication')
        return cleaned_data


class RouterGroupForm(forms.ModelForm):
    class Meta:
        model = RouterGroup
        fields = ['name', 'default_group', 'internal_notes', 'routers']
        widgets = {
            'internal_notes': forms.Textarea(attrs={'rows': 4, 'cols': 40}),  # Define como um Textarea simples
        }

    def __init__(self, *args, **kwargs):
        super(RouterGroupForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        if self.instance.pk:
            delete_html = "<a href='javascript:void(0)' class='btn btn-outline-danger' data-command='delete' onclick='openCommandDialog(this)'>Delete</a>"
        else:
            delete_html = ''
        self.helper.layout = Layout(
            'name',
            'internal_notes',
            'routers',
            'default_group',
            Row(
                Column(
                    Submit('submit', 'Save', css_class='btn btn-success'),
                    HTML(' <a class="btn btn-secondary" href="/router/group_list/">Back</a> '),
                    HTML(delete_html),
                    css_class='col-md-12'),
                css_class='form-row'
            )
        )

    def clean(self):
        cleaned_data = super().clean()
        name = cleaned_data.get('name')

        if name:
            name = name.strip()
            cleaned_data['name'] = name

        return cleaned_data

    def save(self, commit=True):
        group = super().save(commit=commit)

        # Only one group can be the default, so the others give it up. This belongs
        # in save() and not in clean(): clean() runs before the form has decided
        # whether it accepts the input at all, so a duplicate name - which
        # _post_clean rejects - would have taken the default away from another
        # group without saving anything, and the change log would record a change
        # that never happened.
        #
        # One save() per group, and not a queryset update(): update() sends no
        # signal, so the log would show the new default being set and let the old
        # one silently go false.
        if commit and group.default_group:
            for other in RouterGroup.objects.filter(default_group=True).exclude(pk=group.pk):
                other.default_group = False
                other.save()
        return group


class SSHKeyForm(forms.ModelForm):
    class Meta:
        model = SSHKey
        fields = ['name', 'public_key', 'private_key']
        widgets = {
            'public_key': forms.Textarea(attrs={'rows': 4, 'cols': 40}),
            'private_key': forms.Textarea(attrs={'rows': 4, 'cols': 40}),
        }

    def __init__(self, *args, **kwargs):
        super(SSHKeyForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        if self.instance.pk:
            delete_html = "<a href='javascript:void(0)' class='btn btn-outline-danger' data-command='delete' onclick='openCommandDialog(this)'>Delete</a>"
        else:
            delete_html = ''
        self.helper.layout = Layout(
            Row(
                Column('name', css_class='form-group col-md-12 mb-0'),
            ),
            Row(
                Column('public_key', css_class='form-group col-md-12 mb-0'),
            ),
            Row(
                Column('private_key', css_class='form-group col-md-12 mb-0'),
            ),
            Row(
                Column(
                    Submit('submit', 'Save', css_class='btn btn-success'),
                    HTML(' <a class="btn btn-secondary" href="/router/ssh_keys/">Back</a> '),
                    HTML(delete_html),
                    css_class='col-md-12'),
                css_class='form-row'
            )
        )

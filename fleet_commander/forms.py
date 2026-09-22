from crispy_forms.helper import FormHelper
from crispy_forms.layout import Layout, Submit, Div, Field, HTML, Row, Column
from django import forms

from router_manager.models import Router
from router_manager.models import SUPPORTED_ROUTER_TYPES
from .models import Command, CommandVariant, CommandSchedule, ScheduleDefaults, parse_interval

# How a moment is written into a datetime-local input, and read back out of one.
# The widget would otherwise use the first localized input format, which is
# "2026-09-22 03:00:00" - a value that input refuses, leaving the field empty.
DATETIME_LOCAL_FORMAT = '%Y-%m-%dT%H:%M'


class CommandForm(forms.ModelForm):
    class Meta:
        model = Command
        fields = ['name', 'description', 'enabled', 'capture_output', 'max_retry', 'retry_interval',
                  'verify_timeout', 'verify_interval']

    def __init__(self, *args, **kwargs):
        super(CommandForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'

        self.fields['max_retry'].help_text = (
            'Maximum number of attempts if the command fails, the first run included. '
            'A command with a verification executes its payload once, its attempts at verifying '
            'are bounded by the verify timeout instead.'
        )

        if self.instance.pk:
            back_url = f'/fleet_commander/command/details/?uuid={self.instance.uuid}'
            delete_html = (
                "<a href='javascript:void(0)' class='btn btn-outline-danger' "
                "data-command='delete' onclick='openCommandDialog(this)'>Delete</a>"
            )
        else:
            back_url = '/fleet_commander/'
            delete_html = ''

        self.helper.layout = Layout(
            Div(
                Div(Field('name'), css_class='col-md-12'),
                css_class='row',
            ),
            Div(
                Div(Field('description'), css_class='col-md-12'),
                css_class='row',
            ),
            Div(
                Div(Field('max_retry'), css_class='col-md-6'),
                Div(Field('retry_interval'), css_class='col-md-6'),
                css_class='row',
            ),
            Div(
                Div(Field('verify_timeout'), css_class='col-md-6'),
                Div(Field('verify_interval'), css_class='col-md-6'),
                css_class='row',
            ),
            Div(
                Div(Field('capture_output'), css_class='col-md-12'),
                css_class='row',
            ),
            Div(
                Div(Field('enabled'), css_class='col-md-12'),
                css_class='row',
            ),
            Row(
                Column(
                    Submit('submit', 'Save', css_class='btn btn-success'),
                    HTML(f' <a class="btn btn-secondary" href="{back_url}">Back</a> '),
                    HTML(delete_html),
                    css_class='col-md-12'
                )
            ),
        )


class CommandExecuteForm(forms.Form):
    routers = forms.ModelMultipleChoiceField(
        queryset=Router.objects.filter(enabled=True).order_by('name'),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'selectmultiple'})
    )
    router_groups = forms.ModelMultipleChoiceField(
        queryset=forms.Field().initial,  # Placeholder, will be set in __init__
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'selectmultiple'})
    )

    def __init__(self, *args, command=None, **kwargs):
        from router_manager.models import RouterGroup
        super(CommandExecuteForm, self).__init__(*args, **kwargs)
        self.command = command
        self.fields['router_groups'].queryset = RouterGroup.objects.all().order_by('name')
        
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        
        back_url = f'/fleet_commander/command/details/?uuid={command.uuid}' if command else '/fleet_commander/'
        
        self.helper.layout = Layout(
            Div(
                Div(Field('routers'), css_class='col-md-6'),
                Div(Field('router_groups'), css_class='col-md-6'),
                css_class='row',
            ),
            Row(
                Column(
                    Submit('submit', 'Execute Now', css_class='btn btn-primary'),
                    HTML(f' <a class="btn btn-secondary" href="{back_url}">Back</a> '),
                    css_class='col-md-12'
                )
            ),
        )


class CommandVariantForm(forms.ModelForm):
    class Meta:
        model = CommandVariant
        fields = ['router_type', 'payload', 'verify_payload', 'verify_expect', 'enabled']

    def __init__(self, *args, command=None, **kwargs):
        super(CommandVariantForm, self).__init__(*args, **kwargs)
        self.command = command
        
        existing_types = []
        if self.command:
            existing_types = list(
                CommandVariant.objects.filter(command=self.command)
                .exclude(pk=self.instance.pk if self.instance.pk else None)
                .values_list('router_type', flat=True)
            )

        valid_choices = [c for c in SUPPORTED_ROUTER_TYPES if c[0] != 'monitoring' and c[0] not in existing_types]
        self.fields['router_type'].choices = [('', '---------')] + valid_choices

        self.fields['verify_payload'].required = False
        self.fields['verify_payload'].label = 'Verification commands'
        self.fields['verify_payload'].widget.attrs['rows'] = 4
        self.fields['verify_payload'].help_text = (
            'Optional. Commands that check the result of the payload, one command per line. '
            'They are executed after the payload and decide whether the task was successful.'
        )
        self.fields['verify_expect'].required = False
        self.fields['verify_expect'].label = 'Expected result'
        self.fields['verify_expect'].widget.attrs['rows'] = 4
        self.fields['verify_expect'].help_text = (
            'One expectation per line. Every line has to be found in the output of the verification commands. '
            '{{ available_version }} and {{ current_version }} are replaced with the versions known for the router. '
            '{{ expected_version }} is the version the router offers on the channel the payload set, taken from a '
            'line the payload prints as "expected-version=...". Use it when the payload changes the update channel, '
            'the version known to RouterFleet belongs to the channel from before.'
        )

        self.helper = FormHelper()
        self.helper.form_method = 'post'

        if self.instance.pk:
            back_uuid = self.instance.command.uuid
            delete_html = (
                "<a href='javascript:void(0)' class='btn btn-outline-danger' "
                "data-command='delete' onclick='openCommandDialog(this)'>Delete</a>"
            )
        else:
            back_uuid = command.uuid if command else ''
            delete_html = ''

        self.helper.layout = Layout(
            Div(
                Div(Field('router_type'), css_class='col-md-12'),

                css_class='row',
            ),
            Div(
                Div(Field('payload'), css_class='col-md-12'),
                css_class='row',
            ),
            Div(
                Div(Field('verify_payload'), css_class='col-md-6'),
                Div(Field('verify_expect'), css_class='col-md-6'),
                css_class='row',
            ),
            Div(
                Div(Field('enabled'), css_class='col-md-12'),
                css_class='row',
            ),
            Row(
                Column(
                    Submit('submit', 'Save', css_class='btn btn-success'),
                    HTML(f' <a class="btn btn-secondary" href="/fleet_commander/command/details/?uuid={back_uuid}">Back</a> '),
                    HTML(delete_html),
                    css_class='col-md-12'
                )
            ),
        )

    def clean(self):
        cleaned_data = super().clean()
        router_type = cleaned_data.get('router_type')

        if router_type and self.command:
            query = CommandVariant.objects.filter(command=self.command, router_type=router_type)
            if self.instance.pk:
                query = query.exclude(pk=self.instance.pk)
            if query.exists():
                self.add_error('router_type', 'A variant for this router type already exists for this command.')

        verify_payload = (cleaned_data.get('verify_payload') or '').strip()
        verify_expect = (cleaned_data.get('verify_expect') or '').strip()
        if bool(verify_payload) != bool(verify_expect):
            self.add_error(
                'verify_expect',
                'A verification needs both the verification commands and the expected result.'
            )

        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        if self.command:
            instance.command = self.command
        if commit:
            instance.save()
        return instance


class CommandScheduleForm(forms.ModelForm):
    repeat_interval = forms.CharField(label='Repeat Interval', initial='7d', required=False)

    class Meta:
        model = CommandSchedule
        fields = ['enabled', 'router', 'router_group', 'exclude_router',
                  'exclude_router_group', 'start_at', 'end_at']
        widgets = {
            'start_at': forms.DateTimeInput(attrs={'type': 'datetime-local'},
                                            format=DATETIME_LOCAL_FORMAT),
            'end_at': forms.DateTimeInput(attrs={'type': 'datetime-local'},
                                          format=DATETIME_LOCAL_FORMAT),
        }

    def __init__(self, *args, command=None, **kwargs):
        super(CommandScheduleForm, self).__init__(*args, **kwargs)
        self.command = command
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        self.fields['start_at'].required = True
        for name in ('start_at', 'end_at'):
            # The value a datetime-local input sends is written the same way it is
            # shown, so it is read back that way as well - before the localized
            # formats, which do not know the "T"
            self.fields[name].input_formats = [DATETIME_LOCAL_FORMAT] + list(
                self.fields[name].input_formats)

        if self.instance.pk:
            # A schedule that exists shows what it was saved with, also when that is
            # a single run
            self.fields['repeat_interval'].initial = self.instance.repeat_interval_display
        else:
            # A schedule that is created starts out as the global defaults say.
            # One that exists keeps what it was saved with.
            defaults = ScheduleDefaults.load()
            self.fields['start_at'].initial = defaults.next_start_at()
            self.fields['repeat_interval'].initial = defaults.repeat_interval_display

        if self.instance.pk:
            back_uuid = self.instance.command.uuid
            delete_html = (
                "<a href='javascript:void(0)' class='btn btn-outline-danger' "
                "data-command='delete' onclick='openCommandDialog(this)'>Delete</a>"
            )
        else:
            back_uuid = command.uuid if command else ''
            delete_html = ''

        self.helper.layout = Layout(
            Div(
                Div(Field('router'), css_class='col-xl-6'),
                Div(Field('router_group'), css_class='col-xl-6'),
                css_class='row',
            ),
            Div(
                Div(Field('exclude_router'), css_class='col-xl-6'),
                Div(Field('exclude_router_group'), css_class='col-xl-6'),
                css_class='row',
            ),
            Div(
                Div(Field('start_at'), css_class='col-xl-6'),
                Div(Field('end_at'), css_class='col-xl-6'),
                Div(Field('repeat_interval'), css_class='col-xl-6'),
                css_class='row',
            ),
            Div(
                Div(Field('enabled'), css_class='col-md-12'),
                css_class='row',
            ),
            Row(
                Column(
                    Submit('submit', 'Save', css_class='btn btn-success'),
                    HTML(f' <a class="btn btn-secondary" href="/fleet_commander/command/details/?uuid={back_uuid}">Back</a> '),
                    HTML(delete_html),
                    css_class='col-md-12'
                )
            ),
        )

    def clean_repeat_interval(self):
        try:
            return parse_interval(self.cleaned_data.get('repeat_interval'))
        except ValueError as error:
            raise forms.ValidationError(str(error))

    def clean(self):
        cleaned_data = super().clean()
        routers = set(cleaned_data.get('router') or [])
        router_group = cleaned_data.get('router_group') or []
        if not routers and not router_group:
            raise forms.ValidationError("You must select at least one router or one router group.")

        # An exclusion wins over everything, so a schedule whose every device is
        # excluded would never run anything - that is worth saying before it is
        # saved instead of leaving a schedule behind that silently does nothing
        for group in router_group:
            routers.update(group.routers.all())
        excluded = set(cleaned_data.get('exclude_router') or [])
        for group in cleaned_data.get('exclude_router_group') or []:
            excluded.update(group.routers.all())
        if not routers - excluded:
            raise forms.ValidationError(
                "Every device of this schedule is excluded from it, so it would never run anything.")
        return cleaned_data

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.repeat_interval = self.cleaned_data.get('repeat_interval', 0)
        if self.command:
            instance.command = self.command
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class ScheduleDefaultsForm(forms.ModelForm):
    """The values a new schedule is created with. It changes nothing about the
    schedules that already exist."""

    start_time = forms.TimeField(
        label='Start Time', input_formats=['%H:%M', '%H:%M:%S'],
        widget=forms.TimeInput(attrs={'type': 'time'}, format='%H:%M'),
        help_text='The time of day a new schedule begins at. It starts at the next '
                  'time this comes around.')
    repeat_interval = forms.CharField(
        label='Repeat Interval', initial='7d', required=False,
        help_text="How often a new schedule repeats: 7d, 24h, 30m. Leave it empty "
                  "or set it to 0 for a single run.")

    class Meta:
        model = ScheduleDefaults
        fields = ['start_time', 'repeat_interval']

    def __init__(self, *args, **kwargs):
        super(ScheduleDefaultsForm, self).__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = 'post'
        if self.instance.pk:
            # The model holds minutes, the form is filled with the interval as it is
            # written. It goes into initial, not into the field: what comes from the
            # instance would win over the field either way.
            self.initial['repeat_interval'] = self.instance.repeat_interval_display

        self.helper.layout = Layout(
            Div(
                Div(Field('start_time'), css_class='col-md-6'),
                Div(Field('repeat_interval'), css_class='col-md-6'),
                css_class='row',
            ),
            Row(
                Column(
                    Submit('submit', 'Save', css_class='btn btn-success'),
                    HTML(' <a class="btn btn-secondary" href="/fleet_commander/">Back</a> '),
                    css_class='col-md-12'
                )
            ),
        )

    def clean_repeat_interval(self):
        try:
            return parse_interval(self.cleaned_data.get('repeat_interval'))
        except ValueError as error:
            raise forms.ValidationError(str(error))

import decimal
import re
from collections import OrderedDict
from distutils.version import StrictVersion

from django.utils.functional import cached_property
from django.utils.translation import to_locale, get_language
from django import forms
from django.core.exceptions import FieldError, ValidationError
from django.db import models
from django.db.models import NOT_PROVIDED
from django.forms.models import ModelFormMetaclass
from django.utils.encoding import force_text
import money

try:
    import babel
    import babel.numbers
    BABEL_VERSION = StrictVersion(babel.__version__)
    LC_NUMERIC = babel.default_locale('LC_NUMERIC')
except ImportError:
    babel = LC_NUMERIC = None


from moneyfield.exceptions import *

__all__ = ['MoneyField', 'MoneyModelForm', 'MoneyLC']


REGEX_CURRENCY_CODE = re.compile("^[A-Z]{3}$")


class MoneyLC(money.Money):

    @cached_property
    def language_locale(self):
        return to_locale(get_language())

    @property
    def _amount_00_prec(self):
        """Two decimal places down for better accuracy in monetary values.
        1.9867272 -> 1.98
        """
        return self._amount.quantize(decimal.Decimal('.00'), rounding=decimal.ROUND_DOWN)

    def format(self, locale=LC_NUMERIC, pattern=None, currency_digits=True,
               format_type='standard', **options):
        if babel:
            if BABEL_VERSION < StrictVersion('2.2'):
                raise Exception('Babel {} is unsupported. '
                                'Please upgrade to 2.2 or higher.'.format(BABEL_VERSION))
            return babel.numbers.format_currency(
                self._amount_00_prec, self._currency, format=pattern, locale=locale,
                currency_digits=currency_digits, format_type=format_type,
                **options)
        else:
            raise NotImplementedError("formatting requires Babel "
                                      "(https://pypi.python.org/pypi/Babel)")

    def __str__(self):
        if babel:
            # noinspection PyBroadException
            try:
                return self.format(self.language_locale,
                                   decimal_quantization=False)
            except Exception as exc:
                return super().__str__()
        else:
            return super().__str__()


def currency_code_validator(value):
    if not REGEX_CURRENCY_CODE.match(force_text(value)):
        raise ValidationError('Invalid currency code.')


class MoneyModelFormMetaclass(ModelFormMetaclass):
    def __new__(cls, name, bases, attrs):
        new_class = super().__new__(cls, name, bases, attrs)
        if name == 'MoneyModelForm':
            return new_class

        money_fields = set()
        # will support at most one level of inheritance.
        for model_cls in new_class._meta.model.mro()[:2]:
            if (model_opts := getattr(model_cls, '_meta', None)) and hasattr(model_opts, 'money_fields'):
                money_fields.update(model_opts.money_fields)

        model_opts = new_class._meta.model._meta
        model_opts.money_fields = money_fields

        if not model_opts.money_fields:
            raise MoneyModelFormError("The Model used with this ModelForm "
                                      "does not contain MoneyFields")

        # Rebuild the dict of form fields by replacing fields derived from
        # money subfields with a specialised money multivalue form field,
        # while preserving the original ordering.
        fields = OrderedDict()
        for field_name, field in new_class.base_fields.items():
            for money_field in model_opts.money_fields:
                if field_name == money_field.amount_attr:
                    fields[money_field.name] = money_field.formfield()
                    break
                if field_name == money_field.currency_attr:
                    break
            else:
                fields[field_name] = field
        
        new_class.base_fields = fields
        return new_class


class MoneyModelForm(forms.ModelForm, metaclass=MoneyModelFormMetaclass):
    def __init__(self, *args, initial: dict = None, instance=None, **kwargs):
        opts = self._meta
        model_opts = opts.model._meta
        if initial is None:
            initial = {}
        if instance:
            # Populate the multivalue form field using the initial dict,
            # as model_to_dict() only sees the model's _meta.fields
            for money_field in model_opts.money_fields:
                initial.update({
                    money_field.name: getattr(instance, money_field.name)}
                )
        
        super().__init__(*args, initial=initial, instance=instance, **kwargs)
        
        # Money "subfields" cannot be excluded separately
        if opts.exclude:
            for money_field in model_opts.money_fields:
                if not money_field.fixed_currency:
                    if not ((money_field.amount_attr in opts.exclude) == 
                            (money_field.currency_attr in opts.exclude)):
                        msg = ('Cannot exclude only one money field '
                               'from the model form.')
                        raise MoneyModelFormError(msg)
    
    def clean(self):
        cleaned_data = super().clean()
        # Finish the work of forms.models.construct_instance() as it doesn't
        # find match between the form multivalue field (e.g. "price"), and the
        # model's _meta.fields (e.g. "price_amount" and "price_currency").
        opts = self._meta
        model_opts = opts.model._meta
        for money_field in model_opts.money_fields:
            if money_field.name in self.cleaned_data:
                value = self.cleaned_data[money_field.name]
                if value:
                    setattr(self.instance, money_field.name, value)
        
        return cleaned_data


class MoneyWidget(forms.MultiWidget):
    def decompress(self, value):
        if isinstance(value, money.Money):
            return [value.amount, value.currency]
        if value is None:
            return [None, None]
        raise TypeError('MoneyWidgets accept only Money.')
    
    def format_output(self, rendered_widgets):
        return ' '.join(rendered_widgets)
    
    def value_from_datadict(self, data, files, name):
        # Enable datadict value to be compressed
        if name in data:
            return self.decompress(data[name])
        else:
            return super().value_from_datadict(data, files, name)


class MoneyFormField(forms.MultiValueField):
    def __init__(self, fields=(), *args, **kwargs):
        if not kwargs.setdefault('initial'):
            kwargs['initial'] = [f.initial for f in fields]
        super().__init__(*args, fields=fields, **kwargs)

    def compress(self, data_list):
        if data_list:
            return MoneyLC(data_list[0], data_list[1])
        return None


class FixedCurrencyWidget(forms.Widget):
    template_name: str = "moneyfield/widgets/fixed_currency.html"

    def __init__(self, attrs=None, currency=None):
        assert currency
        super().__init__(attrs=attrs)
        self.currency = currency
    
    def value_from_datadict(self, data, files, name):
        # Defaults to fixed currency
        value = super().value_from_datadict(data, files, name)
        return value or self.currency

    def get_context(self, name, value, attrs, **kwargs):
        if value and value != self.currency:
            msg = ('FixedCurrencyWidget "{}" with fixed currency "{}" '
                   'cannot be rendered with currency "{}".')
            raise TypeError(msg.format(name, self.currency, value))
        attrs = self.build_attrs(attrs, {
            'style': 'vertical-align: middle;'
        })
        context = super().get_context(name, value, attrs, **kwargs)
        context['widget']['currency'] = self.currency
        return context


class FixedCurrencyFormField(forms.Field):
    def __init__(self, currency=None, *args, **kwargs):
        assert currency
        self.currency = currency
        kwargs.setdefault("widget", FixedCurrencyWidget(currency=currency))
        super().__init__(*args, **kwargs)
    
    def validate(self, value):
        if value is not self.currency:
            msg = 'Invalid currency "{}" for "{}"-only FixedCurrencyFormField'
            raise ValidationError(msg.format(value, self.currency))


class AbstractMoneyProxy:
    """Object descriptor for MoneyFields"""
    def __init__(self, field):
        self.field = field
    
    def _get_values(self, obj):
        raise NotImplementedError()
    
    def _set_values(self, obj, amount, currency):
        raise NotImplementedError()
    
    def __get__(self, obj, model):
        """Return a Money object if called in a model instance"""
        if obj is None:
            return self.field
        amount, currency = self._get_values(obj)
        if amount is None or currency is None:
            return None
        return MoneyLC(amount, currency)
    
    def __set__(self, obj, value):
        """Set amount and currency attributes in the model instance"""
        if isinstance(value, money.Money):
            self._set_values(obj, value.amount, value.currency)
        elif value is None:
            self._set_values(obj, None, None)
        elif self.field.fixed_currency and not self.field.amount_proxy and isinstance(value, decimal.Decimal):
            self._set_values(obj, value)
        else:
            msg = 'Cannot assign "{}" to MoneyField "{}".'
            raise TypeError(msg.format(type(value), self.field.name))


class SimpleMoneyProxy(AbstractMoneyProxy):
    """Descriptor for MoneyFields with fixed currency"""
    def _get_values(self, obj):
        return (obj.__dict__[self.field.amount_attr],
                self.field.fixed_currency)
    
    def _set_values(self, obj, amount, currency=None):
        if currency is not None:
            if currency != self.field.fixed_currency:
                raise TypeError('Field "{}" is {}-only.'.format(
                    self.field.name, 
                    self.field.fixed_currency
                ))
        obj.__dict__[self.field.amount_attr] = amount


class CompositeMoneyProxy(AbstractMoneyProxy):
    """Descriptor for MoneyFields with variable currency"""
    def _get_values(self, obj):
        return (obj.__dict__[self.field.amount_attr],
                obj.__dict__[self.field.currency_attr])
    
    def _set_values(self, obj, amount, currency):
        obj.__dict__[self.field.amount_attr] = amount
        obj.__dict__[self.field.currency_attr] = currency


class MoneyDecimalField(models.DecimalField):
    """Make necessary adjustments when MoneyField, has amount_proxy=False"""
    def to_python(self, value):
        if isinstance(value, money.Money):
            return value.amount
        return super().to_python(value)


class MoneyField(models.Field):
    description = "Money"
    
    def __init__(self, verbose_name=None, name=None,
                 max_digits=None, decimal_places=None,
                 currency=None, currency_choices=None,
                 currency_default=NOT_PROVIDED,
                 default=NOT_PROVIDED,
                 amount_default=NOT_PROVIDED,
                 amount_proxy=True,
                 **kwargs):
        
        super().__init__(verbose_name, name, default=default, **kwargs)
        self.fixed_currency = currency
        self.amount_proxy = amount_proxy

        # DecimalField pre-validation
        if decimal_places is None or decimal_places < 0:
            msg = ('"{}": MoneyFields require a non-negative integer '
                   'argument "decimal_places".')
            raise FieldError(msg.format(self.name))
        if max_digits is None or max_digits <= 0:
            msg = ('"{}": MoneyFields require a positive integer '
                   'argument "max_digits".')
            raise FieldError(msg.format(self.name))
        
        # Currency must be either fixed or variable, not both.
        if currency and (currency_choices or currency_default != NOT_PROVIDED):
            msg = ('MoneyField "{}" has fixed currency "{}". '
                   'Do not use "currency_choices" or "currency_default" '
                   'at the same time.')
            raise FieldError(msg.format(self.name, currency))
        
        # Money default
        if default != NOT_PROVIDED:
            if isinstance(default, money.Money):
                # Must be compatible with fixed currency
                if currency and not (currency == default.currency):
                    msg = ('MoneyField "{}" has fixed currency "{}". '
                           'The default value "{}" is not compatible.')
                    raise FieldError(msg.format(self.name, currency, default))
                
                # Do not set other defaults at the same time
                if amount_default != NOT_PROVIDED:
                    msg = ('MoneyField "{}" has a default value "{}". Do not '
                           'use "amount_default" at the same time.')
                    raise FieldError(msg.format(self.name, default))
                
                if currency_default != NOT_PROVIDED:
                    msg = ('MoneyField "{}" has a default value "{}". '
                           'Do not use "currency_default" at the same time.')
                    raise FieldError(msg.format(self.name, default))
                
                amount_default = default.amount
                currency_default = default.currency
            else:
                msg = ('MoneyField "{}" default must be '
                       'of type Money, it is "{}".')
                raise TypeError(msg.format(self.name, type(currency)))

        amount_options = kwargs.copy()
        if self.fixed_currency and not self.amount_proxy:
            amount_options['verbose_name'] = self.verbose_name
            amount_options['name'] = self.name

        self.amount_field = MoneyDecimalField(
            decimal_places=decimal_places,
            max_digits=max_digits,
            default=amount_default,
            **amount_options
        )
        if not self.fixed_currency:
            # This Moneyfield can have different currencies.
            # Add a currency column to the database
            self.currency_field = models.CharField(
                max_length=3,
                default=currency_default,
                choices=currency_choices,
                validators=[currency_code_validator],
                **kwargs
            )
    
    def contribute_to_class(self, cls, name, **kwargs):
        self.name = name
        if self.fixed_currency and not self.amount_proxy:
            self.amount_attr = name
        else:
            self.amount_attr = '{}_amount'.format(name)

        cls.add_to_class(self.amount_attr, self.amount_field)
        
        if not self.fixed_currency:
            self.currency_attr = '{}_currency'.format(name)
            cls.add_to_class(self.currency_attr, self.currency_field)
            setattr(cls, name, CompositeMoneyProxy(self))
        else:
            self.currency_attr = None
            setattr(cls, name, SimpleMoneyProxy(self))
        
        # Keep a list of MoneyFields in the model's _meta
        # This will help identify which MoneyFields a model has
        if not hasattr(cls._meta, 'money_fields'):
            cls._meta.money_fields = []
        cls._meta.money_fields.append(self)
    
    def formfield(self, **kwargs):
        formfield_amount = self.amount_field.formfield()
        form_initial = [self.amount_field.default]
        if not self.fixed_currency:
            formfield_currency = self.currency_field.formfield(
                validators=[currency_code_validator]
            )
            form_initial.append(self.currency_field.default)
        else:
            form_initial.append(self.fixed_currency)
            formfield_currency = FixedCurrencyFormField(
                currency=self.fixed_currency
            )

        widget_amount = formfield_amount.widget
        widget_currency = formfield_currency.widget

        # Adjust currency input size
        if type(widget_currency) is forms.TextInput:
            widget_currency.attrs.update({'size': 3})

        config = {
            'fields': (formfield_amount, formfield_currency),
            'widget': MoneyWidget(widgets=(widget_amount, widget_currency)),
            'initial': form_initial
        }
        config.update(kwargs)

        return super().formfield(form_class=MoneyFormField, **config)

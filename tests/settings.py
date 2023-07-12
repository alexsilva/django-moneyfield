DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
    }
}

USE_L10N = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

SECRET_KEY = "justthetestapp"

INSTALLED_APPS = (
    'moneyfield',
    'testapp',
)

MONEY_CURRENCY_CHOICES = (
    ('AAA', 'AAA'),
    ('BBB', 'BBB'),
    ('CCC', 'CCC'),
)
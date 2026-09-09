from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import AccessToken

from .models import GoogleIdentity, SignInChallenge, User


@override_settings(GOOGLE_SIGNIN_WEB_CLIENT_ID="test-client")
class EasyAuthTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.mail = patch('users.easy_auth.send_signin_code').start()
        self.addCleanup(patch.stopall)
        self.user = User.objects.create_user(username='existing', email='owner@example.com', password='old-password')

    def start(self, email='owner@example.com'):
        response = self.client.post('/api/users/signin/email/', {'email': email})
        self.assertEqual(response.status_code, 200, response.data)
        self.code = self.mail.call_args.args[1]
        self.challenge = response.data['challenge_id']
        return response

    def verify(self, **fields):
        return self.client.post('/api/users/signin/verify/',
            {'challenge_id': self.challenge, 'code': self.code, **fields})

    def test_existing_account_preserved_and_code_single_use(self):
        self.user.token_version = 7
        self.user.save()
        self.start('OWNER@example.com')
        row = SignInChallenge.objects.get()
        self.assertNotEqual(row.code_hash, self.code)
        response = self.verify()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['user']['id'], self.user.id)
        self.assertEqual(AccessToken(response.data['access'])['tv'], 7)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(self.verify().status_code, 400)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('old-password'))

    def test_new_account_asks_username_after_email_proof(self):
        self.start('new@example.com')
        self.assertEqual(self.verify().data, {'needs_username': True})
        self.assertEqual(User.objects.count(), 1)
        response = self.verify(username='new-user')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.get(username='new-user').has_usable_password())

    def test_wrong_guesses_lock_even_when_correct_code_follows(self):
        self.start()
        wrong = '000000' if self.code != '000000' else '111111'
        for _ in range(5):
            self.assertEqual(self.verify(code=wrong).status_code, 400)
        self.assertEqual(self.verify().status_code, 400)
        self.assertEqual(SignInChallenge.objects.get().attempts, 5)

    def test_expiration(self):
        self.start()
        SignInChallenge.objects.update(expires_at=timezone.now()-timedelta(seconds=1))
        self.assertEqual(self.verify().status_code, 400)

    def test_resend_cooldown_and_old_code_invalidation(self):
        self.start()
        previous = self.challenge
        self.assertEqual(self.client.post('/api/users/signin/email/', {'email': self.user.email}).status_code, 429)
        SignInChallenge.objects.update(last_sent_at=timezone.now()-timedelta(seconds=61))
        self.start()
        self.assertNotEqual(previous, self.challenge)
        self.assertEqual(self.verify(challenge_id=previous).status_code, 400)

    def test_hourly_email_limit_survives_new_requests(self):
        self.start()
        SignInChallenge.objects.update(send_count=5, last_sent_at=timezone.now()-timedelta(seconds=61))
        self.assertEqual(self.client.post('/api/users/signin/email/', {'email': self.user.email}).status_code, 429)

    def test_disabled_and_ambiguous_email_accounts_do_not_get_tokens(self):
        self.user.is_active = False
        self.user.save()
        self.start()
        self.assertEqual(self.verify().status_code, 403)
        User.objects.create_user(username='duplicate', email=self.user.email)
        self.assertEqual(self.verify().status_code, 409)

    def test_username_collision_is_recoverable_without_losing_email_proof(self):
        self.start('new@example.com')
        self.assertEqual(self.verify(username='EXISTING').status_code, 409)
        self.assertEqual(self.verify(username='available').status_code, 200)

    def test_delivery_failure_does_not_leave_a_usable_code(self):
        self.mail.side_effect = RuntimeError('mail unavailable')
        response = self.client.post('/api/users/signin/email/', {'email': self.user.email})
        self.assertEqual(response.status_code, 503)
        self.assertTrue(SignInChallenge.objects.get().consumed)
        self.assertEqual(SignInChallenge.objects.get().code_hash, '')

    @patch('users.easy_auth.verify_google_token', return_value=('subject-1', 'owner@example.com'))
    def test_google_links_only_after_email_code_and_reuses_account(self, verify):
        response = self.client.post('/api/users/signin/google/', {'id_token': 'credential'})
        self.assertTrue(response.data['google_link'])
        self.assertFalse(GoogleIdentity.objects.exists())
        self.challenge = response.data['challenge_id']
        self.code = self.mail.call_args.args[1]
        self.assertEqual(self.verify().data['user']['id'], self.user.id)
        self.assertEqual(GoogleIdentity.objects.get().user_id, self.user.id)
        response = self.client.post('/api/users/signin/google/', {'id_token': 'credential'})
        self.assertIn('access', response.data)
        self.assertEqual(User.objects.count(), 1)

    @patch('users.easy_auth.verify_google_token', return_value=('other-subject', 'owner@example.com'))
    def test_google_cannot_replace_an_existing_link(self, verify):
        GoogleIdentity.objects.create(subject='original-subject', user=self.user)
        response = self.client.post('/api/users/signin/google/', {'id_token': 'credential'})
        self.challenge = response.data['challenge_id']
        self.code = self.mail.call_args.args[1]
        self.assertEqual(self.verify().status_code, 409)
        self.assertEqual(GoogleIdentity.objects.get().subject, 'original-subject')

    @patch('users.easy_auth.verify_google_token', side_effect=ValueError('invalid'))
    def test_invalid_google_credentials_do_not_send_email(self, verify):
        self.assertEqual(self.client.post('/api/users/signin/google/', {'id_token': 'bad'}).status_code, 400)
        self.mail.assert_not_called()

    @override_settings(GOOGLE_SIGNIN_WEB_CLIENT_ID='')
    def test_unconfigured_google_fails_closed(self):
        self.assertEqual(self.client.post('/api/users/signin/google/', {'id_token': 'bad'}).status_code, 503)

    def test_password_login_still_works(self):
        response = self.client.post('/api/users/token/', {'username': self.user.username, 'password': 'old-password'})
        self.assertEqual(response.status_code, 200)

    def test_cleanup_keeps_current_rate_limits(self):
        from .tasks import cleanup_signin_challenges
        self.start()
        self.assertEqual(cleanup_signin_challenges()['deleted'], 0)
        SignInChallenge.objects.update(last_sent_at=timezone.now()-timedelta(days=3),
                                       expires_at=timezone.now()-timedelta(days=2))
        self.assertEqual(cleanup_signin_challenges()['deleted'], 1)

    @patch('google.oauth2.id_token.verify_oauth2_token')
    def test_google_validation_uses_configured_audience_and_requires_verified_email(self, verify):
        from .easy_auth import verify_google_token
        verify.return_value = {'sub': 'google-id', 'email': 'owner@example.com', 'email_verified': True}
        self.assertEqual(verify_google_token('credential'), ('google-id', 'owner@example.com'))
        self.assertEqual(verify.call_args.args[2], 'test-client')
        verify.return_value['email_verified'] = False
        with self.assertRaises(ValueError):
            verify_google_token('credential')
        verify.side_effect = ValueError('expired or invalid signature')
        with self.assertRaises(ValueError):
            verify_google_token('expired')

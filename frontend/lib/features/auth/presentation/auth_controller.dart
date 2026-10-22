import 'dart:async';

import 'package:flutter/foundation.dart' show visibleForTesting;
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/api/dio_client.dart';
import '../../../core/security/biometric_gate.dart';
import '../../../core/security/device_integrity.dart';
import '../../../core/security/inactivity_watcher.dart';
import '../../../core/server/branding.dart';
import '../../../core/server/server_providers.dart';
import '../../../core/storage/app_state_store.dart';
import '../../../core/storage/secure_storage.dart';
import '../../../core/wipe/local_wipe.dart';
import '../../../core/server/company.dart';
import '../data/auth_repository.dart';
import '../domain/auth_user.dart';
import 'auth_state.dart';

final authControllerProvider =
    NotifierProvider<AuthController, AuthState>(AuthController.new);

/// True when the device failed the integrity check (shows a warning banner).
final deviceCompromisedProvider = StateProvider<bool>((ref) => false);

/// Server default for `auth.offline_cache_max_hours`.
const defaultOfflineCacheHours = 72;

class AuthController extends Notifier<AuthState> {
  AuthRepository get _repo => ref.read(authRepositoryProvider);
  TokenStore get _store => ref.read(tokenStoreProvider);

  @override
  AuthState build() {
    final events = ref.read(sessionEventsProvider);
    final sub = events.expired.listen(_onSessionExpired);
    final pwSub = events.passwordChangeRequired.listen((_) => _onPasswordChangeRequired());
    ref.onDispose(() {
      unawaited(sub.cancel());
      unawaited(pwSub.cancel());
    });
    Future<void>.microtask(bootstrap);
    return const AuthInitial();
  }

  /// Splash logic: restore a stored session (biometric gate first if enabled).
  Future<void> bootstrap() async {
    if (ref.read(serverSetupRequiredProvider)) {
      state = const AuthNeedsServer();
      return;
    }
    state = const AuthInitial();
    if (!await _store.hasSession()) {
      state = const AuthUnauthenticated();
      return;
    }
    await _enforceOfflineLimit();
    final bio = await _store.readBiometricEnabled();
    if (bio) {
      final ok = await ref
          .read(biometricGateProvider)
          .authenticate('Unlock Employee Connect');
      if (!ok) {
        await _repo.logout();
        state = const AuthUnauthenticated(message: 'Sign in with your password.');
        return;
      }
    }
    try {
      final user = await _repo.me();
      await _enter(user, biometric: bio);
    } on ApiException catch (e) {
      // Only a definitive auth failure ends the session (L12/B6). Network
      // errors, 429, 5xx and proxy errors keep the tokens so the user can retry.
      final definitive = e.status == 401 ||
          (e.status == 403 && e.code == 'ACCOUNT_INACTIVE') ||
          e.isSessionRevoked;
      if (e.status == 403 && e.code == 'ACCOUNT_INACTIVE') {
        await _accessRemoved();
      } else if (definitive) {
        await _store.clearSession();
        state = AuthUnauthenticated(message: _sessionMessage(e.code));
      } else if (e.status == 403 && e.code == 'PASSWORD_CHANGE_REQUIRED') {
        _onPasswordChangeRequired();
      } else {
        state = AuthInitial(error: e.message);
      }
    }
  }

  /// Re-runs the integrity check (called on every app resume). A newly
  /// compromised device logs an Admin out and shows the blocked screen.
  Future<void> recheckIntegrity() async {
    final s = state;
    final AuthUser user;
    if (s is AuthAuthenticated) {
      user = s.user;
    } else if (s is AuthLocked) {
      user = s.user;
    } else {
      return;
    }
    final compromised = await ref.read(deviceIntegrityProvider).isCompromised();
    ref.read(deviceCompromisedProvider.notifier).state = compromised;
    if (compromised && user.isAdmin) {
      await _repo.logout();
      state = const AuthBlocked();
    }
  }

  /// `auth.offline_cache_max_hours`: when no authenticated call succeeded for
  /// that long, cached data is wiped on open. The user then needs a
  /// successful online `/auth/me` (done right after) before data is shown.
  ///
  /// A missing stored value means the server default (72 h), not "off";
  /// only an explicit 0 disables the limit. Returns true when it tripped.
  Future<bool> _enforceOfflineLimit() async {
    try {
      final stored = await ref.read(appStateStoreProvider).readOfflineMaxHours();
      final hours = stored ?? defaultOfflineCacheHours;
      final last = await _store.readLastOk();
      if (hours <= 0 || last == null) return false;
      if (DateTime.now().difference(last) > Duration(hours: hours)) {
        await ref.read(localWipeProvider).cachesOnly();
        return true;
      }
    } on Object {
      // best effort
    }
    return false;
  }

  /// Offline limit check on resume: wipes cached data and locks the UI.
  Future<void> _enforceOfflineLimitOnResume() async {
    final s = state;
    if (s is! AuthAuthenticated && s is! AuthLocked) return;
    if (await _enforceOfflineLimit()) lock();
  }

  /// Offboarding: wipe every local trace (server URL and branding stay) and
  /// show the "access removed" screen.
  Future<void> _accessRemoved() async {
    if (state is AuthAccessRemoved) return;
    state = const AuthAccessRemoved();
    await ref.read(localWipeProvider).all(keepServer: true);
  }

  /// Asks the server whether the account is still valid (resume, or an
  /// `account_disabled` push). A push alone never wipes: only a 403
  /// `ACCOUNT_INACTIVE` answer does (handled by the API client / [bootstrap]).
  Future<void> verifyAccount() async {
    final s = state;
    if (s is! AuthAuthenticated && s is! AuthLocked) return;
    try {
      await _repo.me();
    } on ApiException catch (e) {
      if (e.status == 403 && e.code == 'ACCOUNT_INACTIVE') await _accessRemoved();
    }
  }

  /// App came to the foreground: re-check the account when the last
  /// successful call is older than 60 seconds.
  Future<void> onResume() async {
    await _enforceOfflineLimitOnResume();
    final last = ref.read(sessionEventsProvider).lastSuccess;
    if (last != null && DateTime.now().difference(last) < const Duration(seconds: 60)) {
      return;
    }
    await verifyAccount();
  }

  /// "Change server": wipes everything including branding.
  Future<void> changeServer() async {
    try {
      await _repo.logout();
    } on Object {
      // best effort
    }
    await ref.read(localWipeProvider).all(keepServer: false);
    ref.read(serverSetupRequiredProvider.notifier).state = true;
    state = const AuthNeedsServer();
  }

  /// Server confirmed on the setup screen: remember it and go to login.
  Future<void> serverChosen(String baseUrl, CompanyInfo company) async {
    await ref.read(appStateStoreProvider).writeServerUrl(baseUrl);
    ref.read(baseUrlProvider.notifier).state = baseUrl;
    ref.read(serverSetupRequiredProvider.notifier).state = false;
    state = const AuthUnauthenticated();
    await ref.read(brandingProvider.notifier).update(company);
  }

  /// The setup wizard saved the company: stop forcing the wizard.
  void companyConfigured(CompanyInfo c) {
    final s = state;
    if (s is AuthAuthenticated) {
      state = AuthAuthenticated(
        s.user.copyWith(company: c),
        biometricEnabled: s.biometricEnabled,
      );
      unawaited(ref.read(brandingProvider.notifier).update(c));
    }
  }

  /// Login screen: refresh the cached company name / logo (public call).
  Future<void> refreshPublicBranding() async {
    final c = await _repo.publicCompany();
    if (c != null) await ref.read(brandingProvider.notifier).update(c);
  }

  Future<void> login(String employeeId, String password) async {
    final user = await _repo.login(employeeId, password);
    await _enter(user, biometric: false);
  }

  Future<void> changePassword(String current, String next) async {
    final user = await _repo.changePassword(current, next);
    // Biometric unlock is tied to the old session; require re-enabling.
    await _store.writeBiometricEnabled(enabled: false);
    await _enter(user, biometric: false);
  }

  /// Re-reads `/auth/me` so permissions edited since sign-in are current.
  /// Best effort: any failure keeps the current user (session-ending errors are
  /// handled by the API client).
  Future<void> refreshUser() async {
    if (state is! AuthAuthenticated) return;
    try {
      final user = await _repo.me();
      final cur = state;
      if (cur is AuthAuthenticated && cur.user.id == user.id) {
        state = AuthAuthenticated(user, biometricEnabled: cur.biometricEnabled);
        final c = user.company;
        if (c != null) unawaited(ref.read(brandingProvider.notifier).update(c));
      }
    } on Object {
      // keep the current user
    }
  }

  Future<void> logout({bool all = false}) async {
    await _repo.logout(all: all);
    state = const AuthUnauthenticated();
  }

  void lock() {
    final s = state;
    if (s is AuthAuthenticated) {
      state = AuthLocked(s.user, biometricEnabled: s.biometricEnabled);
    }
  }

  Future<bool> unlockWithBiometrics() async {
    final s = state;
    if (s is! AuthLocked) return false;
    final ok = await ref.read(biometricGateProvider).authenticate('Unlock Employee Connect');
    if (ok) state = AuthAuthenticated(s.user, biometricEnabled: s.biometricEnabled);
    return ok;
  }

  /// Returns false if the user cancelled / device unsupported.
  Future<bool> setBiometric({required bool enabled}) async {
    final s = state;
    if (s is! AuthAuthenticated) return false;
    final gate = ref.read(biometricGateProvider);
    if (enabled) {
      if (!await gate.isAvailable()) return false;
      if (!await gate.authenticate('Enable biometric unlock')) return false;
    }
    await _store.writeBiometricEnabled(enabled: enabled);
    state = AuthAuthenticated(s.user, biometricEnabled: enabled);
    return true;
  }

  Future<void> _enter(AuthUser user, {required bool biometric}) async {
    final compromised = await ref.read(deviceIntegrityProvider).isCompromised();
    ref.read(deviceCompromisedProvider.notifier).state = compromised;
    if (compromised && user.isAdmin) {
      await _repo.logout();
      state = const AuthBlocked();
      return;
    }
    if (user.mustChangePassword) {
      state = AuthNeedsPasswordChange(user);
      return;
    }
    state = AuthAuthenticated(user, biometricEnabled: biometric);
    unawaited(_store.writeLastOk(DateTime.now()).catchError((Object _) {}));
    final c = user.company;
    if (c != null) unawaited(ref.read(brandingProvider.notifier).update(c));
    unawaited(_loadConfig());
  }

  Future<void> _loadConfig() async {
    final cfg = await _repo.config();
    if (cfg == null) return;
    final minutes = cfg.inactivityMinutes;
    if (minutes != null && minutes > 0) {
      // L8: clamp so a misconfigured value cannot disable the lock.
      ref.read(inactivityMinutesProvider.notifier).state = minutes.clamp(1, 30);
    }
    final hours = cfg.offlineCacheMaxHours;
    if (hours != null) {
      try {
        await ref.read(appStateStoreProvider).writeOfflineMaxHours(hours);
      } on Object {
        // best effort
      }
    }
    final c = cfg.company;
    if (c != null) await ref.read(brandingProvider.notifier).update(c);
  }

  void _onSessionExpired(String code) {
    if (state is AuthUnauthenticated ||
        state is AuthBlocked ||
        state is AuthAccessRemoved ||
        state is AuthNeedsServer) {
      return;
    }
    if (code == 'ACCOUNT_INACTIVE') {
      unawaited(_accessRemoved());
      return;
    }
    state = AuthUnauthenticated(message: sessionMessage(code));
  }

  /// 403 PASSWORD_CHANGE_REQUIRED mid-session (admin reset), or the refresh
  /// reply said `must_change_password` (B3/B5).
  void _onPasswordChangeRequired() {
    final s = state;
    final AuthUser user;
    if (s is AuthAuthenticated) {
      user = s.user;
    } else if (s is AuthLocked) {
      user = s.user;
    } else {
      return;
    }
    state = AuthNeedsPasswordChange(user.copyWith(mustChangePassword: true));
  }

  /// User-facing text for a session-ending code (B4, B8).
  @visibleForTesting
  static String sessionMessage(String code) => switch (code) {
        'ACCOUNT_INACTIVE' => 'This account is inactive. Contact your administrator.',
        'TOKEN_REUSED' =>
          'This sign-in was used from another place, so it was ended for your '
              'security. Please sign in again and contact your administrator if '
              'this was not you.',
        'SESSION_REVOKED' =>
          'You were signed out because you signed out elsewhere or your '
              'password was changed or reset. Please sign in again.',
        _ => 'Your session has ended. Please sign in again.',
      };

  static String _sessionMessage(String code) => sessionMessage(code);
}

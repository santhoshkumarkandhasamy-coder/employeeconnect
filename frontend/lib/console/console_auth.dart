import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/api/api_exception.dart';
import '../core/api/dio_client.dart' show dioProvider, guardApi, noAuthKey;
import '../core/server/branding.dart';
import '../core/server/company.dart';
import '../features/admin/domain/admin_models.dart' show AdminPerm;
import '../features/admin/presentation/admin_widgets.dart'
    show adminPermissionsProvider, adminSelfIdProvider;
import '../features/auth/domain/auth_user.dart';
import 'console_api.dart';

sealed class ConsoleAuthState {
  const ConsoleAuthState();
}

/// Reload: trying the cookie refresh.
class ConsoleBooting extends ConsoleAuthState {
  const ConsoleBooting();
}

class ConsoleSignedOut extends ConsoleAuthState {
  const ConsoleSignedOut({this.message});
  final String? message;
}

/// First sign-in with a temporary password.
class ConsoleMustChangePassword extends ConsoleAuthState {
  const ConsoleMustChangePassword(this.user);
  final AuthUser user;
}

class ConsoleSignedIn extends ConsoleAuthState {
  const ConsoleSignedIn(this.user);
  final AuthUser user;
}

final consoleTokensProvider = Provider<ConsoleTokens>((ref) => ConsoleTokens());

final consoleAuthProvider =
    NotifierProvider<ConsoleAuthController, ConsoleAuthState>(ConsoleAuthController.new);

/// Inactivity timeout in minutes (`console.inactivity_timeout_minutes`).
final consoleInactivityMinutesProvider = StateProvider<int>((ref) => 15);

/// Providers the shared admin screens read, bound to the console session.
final consolePermissionsProvider = Provider<List<String>>((ref) {
  final s = ref.watch(consoleAuthProvider);
  return s is ConsoleSignedIn ? s.user.permissions : const <String>[];
});

final consoleSelfIdProvider = Provider<String?>((ref) {
  final s = ref.watch(consoleAuthProvider);
  return s is ConsoleSignedIn ? s.user.id : null;
});

/// Overrides to reuse the admin data layer and screens in the console.
List<Override> consoleAdminOverrides(Dio Function(Ref ref) dio) => [
      dioProvider.overrideWith(dio),
      adminPermissionsProvider.overrideWith((ref) => ref.watch(consolePermissionsProvider)),
      adminSelfIdProvider.overrideWith((ref) => ref.watch(consoleSelfIdProvider)),
    ];

class ConsoleAuthController extends Notifier<ConsoleAuthState> {
  Dio get _dio => ref.read(dioProvider);
  ConsoleTokens get _tokens => ref.read(consoleTokensProvider);
  static final Options _public = Options(extra: const {noAuthKey: true});

  @override
  ConsoleAuthState build() {
    Future<void>.microtask(bootstrap);
    return const ConsoleBooting();
  }

  /// Called by the Dio interceptor when the session is gone.
  void sessionEnded(String code) {
    if (state is ConsoleSignedOut) return;
    _tokens.access = null;
    state = ConsoleSignedOut(
      message: code == 'ACCOUNT_INACTIVE'
          ? 'This account is inactive. Contact your administrator.'
          : 'Your session has ended. Please sign in again.',
    );
  }

  void passwordChangeRequired() {
    final s = state;
    if (s is ConsoleSignedIn) {
      state = ConsoleMustChangePassword(s.user.copyWith(mustChangePassword: true));
    }
  }

  /// Page reload: the HttpOnly cookie gives a new access token.
  Future<void> bootstrap() async {
    state = const ConsoleBooting();
    try {
      final res = await _dio.post<Map<String, dynamic>>('/auth/web/refresh', options: _public);
      _tokens.access = res.data?['access_token'] as String?;
      await _enter(mustChange: res.data?['must_change_password'] == true);
    } on DioException {
      _tokens.access = null;
      state = const ConsoleSignedOut();
    } on ApiException {
      _tokens.access = null;
      state = const ConsoleSignedOut();
    }
  }

  Future<void> login(String employeeId, String password) async {
    final data = await guardApi(() async {
      final res = await _dio.post<Map<String, dynamic>>(
        '/auth/web/login',
        data: {'employee_id': employeeId.trim(), 'password': password},
        options: _public,
      );
      return res.data!;
    });
    _tokens.access = data['access_token'] as String?;
    await _enter(mustChange: data['must_change_password'] == true);
  }

  Future<void> changePassword(String current, String next) async {
    final data = await guardApi(() async {
      final res = await _dio.post<Map<String, dynamic>>(
        '/auth/web/change-password',
        data: {'current_password': current, 'new_password': next},
      );
      return res.data!;
    });
    _tokens.access = data['access_token'] as String? ?? _tokens.access;
    await _enter(mustChange: false);
  }

  Future<void> _enter({required bool mustChange}) async {
    final me = await guardApi(() async {
      final res = await _dio.get<Map<String, dynamic>>('/auth/me');
      return AuthUser.fromMe(res.data!);
    });
    if (!me.hasPermission(AdminPerm.consoleAccess)) {
      await logout(message: 'This account cannot use the admin console.');
      return;
    }
    if (mustChange || me.mustChangePassword) {
      state = ConsoleMustChangePassword(me);
      return;
    }
    state = ConsoleSignedIn(me);
    final c = me.company;
    if (c != null) unawaited(ref.read(brandingProvider.notifier).update(c));
    unawaited(_loadTimeout(me));
  }

  Future<void> _loadTimeout(AuthUser me) async {
    if (!me.hasPermission(AdminPerm.settingsManage)) return;
    try {
      final res = await _dio.get<Map<String, dynamic>>('/admin/settings');
      final s = res.data?['settings'];
      if (s is Map && s['console.inactivity_timeout_minutes'] != null) {
        ref.read(consoleInactivityMinutesProvider.notifier).state =
            clampConsoleMinutes(s['console.inactivity_timeout_minutes']);
      }
    } on DioException {
      // keep the default
    }
  }

  /// A `company.manage` holder finished the setup wizard.
  void companyConfigured(CompanyInfo c) {
    final s = state;
    if (s is ConsoleSignedIn) {
      state = ConsoleSignedIn(s.user.copyWith(company: c));
      unawaited(ref.read(brandingProvider.notifier).update(c));
    }
  }

  Future<void> logout({String? message}) async {
    try {
      await _dio.post<void>('/auth/web/logout', options: _public);
    } on DioException {
      // the cookie may already be gone
    }
    _tokens.access = null;
    state = ConsoleSignedOut(message: message);
  }
}

/// Public company block for the login page (name + logo).
final consoleBrandingFetcher = Provider<Future<CompanyInfo?> Function()>(
  (ref) => () async {
    try {
      final res = await ref.read(dioProvider).get<Map<String, dynamic>>(
            '/public/company',
            options: Options(extra: const {noAuthKey: true}),
          );
      return CompanyInfo.parse(res.data);
    } on DioException {
      return null;
    }
  },
);

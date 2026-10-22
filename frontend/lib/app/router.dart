import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/attendance/presentation/attendance_screen.dart';
import '../features/admin/presentation/company_screen.dart' show CompanySetupScreen;
import '../features/auth/presentation/access_removed_screen.dart';
import '../features/auth/presentation/auth_controller.dart';
import '../features/auth/presentation/server_setup_screen.dart';
import '../features/auth/presentation/auth_state.dart';
import '../features/auth/presentation/blocked_screen.dart';
import '../features/auth/presentation/change_password_screen.dart';
import '../features/auth/presentation/lock_screen.dart';
import '../features/auth/presentation/login_screen.dart';
import '../features/auth/presentation/splash_screen.dart';
import '../features/food/presentation/food_screen.dart';
import '../features/home/presentation/home_screen.dart';
import '../features/leave/presentation/leave_screen.dart';
import '../features/more/more_screen.dart';
import 'shell.dart';

abstract final class Routes {
  static const splash = '/splash';
  static const login = '/login';
  static const changePassword = '/change-password';
  static const locked = '/locked';
  static const blocked = '/blocked';
  static const serverSetup = '/server-setup';
  static const accessRemoved = '/access-removed';
  static const companySetup = '/company-setup';
  static const home = '/home';
  static const attendance = '/attendance';
  static const leave = '/leave';
  static const food = '/food';
  static const more = '/more';
}

const _gateRoutes = {
  Routes.splash,
  Routes.login,
  Routes.changePassword,
  Routes.locked,
  Routes.blocked,
  Routes.serverSetup,
  Routes.accessRemoved,
};

/// Pure auth guard: where should [location] go for [state]? (null = stay)
String? redirectFor(AuthState state, String location) {
  switch (state) {
    case AuthInitial():
      return location == Routes.splash ? null : Routes.splash;
    case AuthUnauthenticated():
      return location == Routes.login ? null : Routes.login;
    case AuthNeedsPasswordChange():
      return location == Routes.changePassword ? null : Routes.changePassword;
    case AuthLocked():
      return location == Routes.locked ? null : Routes.locked;
    case AuthBlocked():
      return location == Routes.blocked ? null : Routes.blocked;
    case AuthNeedsServer():
      return location == Routes.serverSetup ? null : Routes.serverSetup;
    case AuthAccessRemoved():
      return location == Routes.accessRemoved ? null : Routes.accessRemoved;
    case AuthAuthenticated(:final user):
      if (user.needsCompanySetup) {
        return location == Routes.companySetup ? null : Routes.companySetup;
      }
      if (location == Routes.companySetup) return Routes.home;
      return _gateRoutes.contains(location) ? Routes.home : null;
  }
}

final routerProvider = Provider<GoRouter>((ref) {
  final refresh = ValueNotifier<int>(0);
  ref.listen<AuthState>(authControllerProvider, (_, __) => refresh.value++);
  ref.onDispose(refresh.dispose);

  final router = GoRouter(
    initialLocation: Routes.splash,
    refreshListenable: refresh,
    redirect: (context, state) =>
        redirectFor(ref.read(authControllerProvider), state.matchedLocation),
    routes: [
      GoRoute(path: Routes.splash, builder: (_, __) => const SplashScreen()),
      GoRoute(path: Routes.login, builder: (_, __) => const LoginScreen()),
      GoRoute(
        path: Routes.changePassword,
        builder: (_, __) => const ChangePasswordScreen(),
      ),
      GoRoute(path: Routes.locked, builder: (_, __) => const LockScreen()),
      GoRoute(path: Routes.blocked, builder: (_, __) => const BlockedScreen()),
      GoRoute(path: Routes.serverSetup, builder: (_, __) => const ServerSetupScreen()),
      GoRoute(path: Routes.accessRemoved, builder: (_, __) => const AccessRemovedScreen()),
      GoRoute(path: Routes.companySetup, builder: (_, __) => const CompanySetupScreen()),
      // Branches are built lazily on first visit.
      StatefulShellRoute.indexedStack(
        builder: (context, state, shell) => AppShell(shell: shell),
        branches: [
          _branch(Routes.home, const HomeScreen()),
          _branch(Routes.attendance, const AttendanceScreen()),
          _branch(Routes.leave, const LeaveScreen()),
          _branch(Routes.food, const FoodScreen()),
          _branch(Routes.more, const MoreScreen()),
        ],
      ),
    ],
  );
  ref.onDispose(router.dispose);
  return router;
});

StatefulShellBranch _branch(String path, Widget screen) => StatefulShellBranch(
      routes: [GoRoute(path: path, builder: (_, __) => screen)],
    );

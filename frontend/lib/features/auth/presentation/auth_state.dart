import '../domain/auth_user.dart';

sealed class AuthState {
  const AuthState();
}

/// Starting up (splash). [error] set when the session check failed offline.
class AuthInitial extends AuthState {
  const AuthInitial({this.error});
  final String? error;
}

class AuthUnauthenticated extends AuthState {
  const AuthUnauthenticated({this.message});
  final String? message;
}

class AuthNeedsPasswordChange extends AuthState {
  const AuthNeedsPasswordChange(this.user);
  final AuthUser user;
}

class AuthAuthenticated extends AuthState {
  const AuthAuthenticated(this.user, {this.biometricEnabled = false});
  final AuthUser user;
  final bool biometricEnabled;
}

class AuthLocked extends AuthState {
  const AuthLocked(this.user, {this.biometricEnabled = false});
  final AuthUser user;
  final bool biometricEnabled;
}

/// Admin on a rooted / jailbroken device.
class AuthBlocked extends AuthState {
  const AuthBlocked();
}

/// No server URL yet (first run) or the user chose "Change server".
class AuthNeedsServer extends AuthState {
  const AuthNeedsServer();
}

/// The account was deactivated / deleted: local data is wiped, show
/// "Your access has been removed".
class AuthAccessRemoved extends AuthState {
  const AuthAccessRemoved();
}

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../features/auth/presentation/auth_controller.dart';
import '../../features/auth/presentation/auth_state.dart';
import '../config/constants.dart';

/// Inactivity timeout in minutes (from /config `auth.inactivity_timeout_minutes`).
final inactivityMinutesProvider =
    StateProvider<int>((ref) => AppConstants.defaultInactivityMinutes);

/// Locks the app after inactivity (pointer-down resets the timer) and fully
/// logs out after a long time in the background.
class InactivityWatcher extends ConsumerStatefulWidget {
  const InactivityWatcher({required this.child, super.key});
  final Widget child;

  @override
  ConsumerState<InactivityWatcher> createState() => _InactivityWatcherState();
}

class _InactivityWatcherState extends ConsumerState<InactivityWatcher>
    with WidgetsBindingObserver {
  Timer? _timer;
  Stopwatch? _backgroundedAt;
  bool _obscured = false;

  bool get _active => ref.read(authControllerProvider) is AuthAuthenticated;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    super.dispose();
  }

  void _reset() {
    _timer?.cancel();
    if (!_active) return;
    final minutes = ref.read(inactivityMinutesProvider);
    _timer = Timer(Duration(minutes: minutes), _onIdle);
  }

  void _onIdle() {
    if (_active) ref.read(authControllerProvider.notifier).lock();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    // M10: blank the UI as soon as the app is not in the foreground so the OS
    // app-switcher snapshot never contains employee data.
    final hide = state != AppLifecycleState.resumed;
    if (hide != _obscured) setState(() => _obscured = hide);
    if (state == AppLifecycleState.resumed) {
      // H2: re-check device integrity on every resume, not only at login.
      unawaited(ref.read(authControllerProvider.notifier).recheckIntegrity());
      // Offboarding: ask the server whether the account is still valid.
      unawaited(ref.read(authControllerProvider.notifier).onResume());
    }
    if (state == AppLifecycleState.paused) {
      // L4: monotonic stopwatch, immune to the user changing the wall clock.
      _backgroundedAt ??= Stopwatch()..start();
    } else if (state == AppLifecycleState.resumed) {
      final since = _backgroundedAt;
      _backgroundedAt = null;
      if (since == null || ref.read(authControllerProvider) is! AuthAuthenticated) {
        return;
      }
      final away = since.elapsed;
      final ctl = ref.read(authControllerProvider.notifier);
      if (away >= AppConstants.backgroundLogout) {
        unawaited(ctl.logout());
      } else if (away >= Duration(minutes: ref.read(inactivityMinutesProvider))) {
        ctl.lock();
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    ref.listen<AuthState>(authControllerProvider, (prev, next) {
      if (next is AuthAuthenticated) {
        _reset();
      } else {
        _timer?.cancel();
      }
    });
    return Listener(
      behavior: HitTestBehavior.translucent,
      onPointerDown: (_) => _reset(),
      child: Stack(
        fit: StackFit.passthrough,
        children: [
          widget.child,
          if (_obscured)
            Positioned.fill(
              child: ColoredBox(color: Theme.of(context).colorScheme.surface),
            ),
        ],
      ),
    );
  }
}

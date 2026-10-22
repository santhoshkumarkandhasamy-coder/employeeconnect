import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/security/screen_protect.dart';
import 'auth_controller.dart';
import 'auth_state.dart';

/// Shown after inactivity. Unlock with biometrics, or sign in again.
class LockScreen extends ConsumerWidget {
  const LockScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final bio = ref.watch(
      authControllerProvider.select((s) => s is AuthLocked && s.biometricEnabled),
    );
    final ctl = ref.read(authControllerProvider.notifier);
    return SecureScreen(
      child: Scaffold(
        body: SafeArea(
          child: Center(
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const Icon(Icons.lock_outline, size: 56),
                    const SizedBox(height: 16),
                    Text(
                      'Session locked',
                      textAlign: TextAlign.center,
                      style: Theme.of(context).textTheme.headlineSmall,
                    ),
                    const SizedBox(height: 8),
                    const Text(
                      'You were inactive for a while.',
                      textAlign: TextAlign.center,
                    ),
                    const SizedBox(height: 32),
                    if (bio) ...[
                      FilledButton.icon(
                        onPressed: ctl.unlockWithBiometrics,
                        icon: const Icon(Icons.fingerprint),
                        label: const Text('Unlock'),
                      ),
                      const SizedBox(height: 12),
                    ],
                    OutlinedButton(
                      onPressed: ctl.logout,
                      style: OutlinedButton.styleFrom(minimumSize: const Size.fromHeight(52)),
                      child: const Text('Sign in with password'),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

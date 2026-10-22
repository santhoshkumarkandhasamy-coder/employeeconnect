import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/server/branding.dart';
import 'auth_controller.dart';

/// Shown after the account was deactivated or deleted. Local data is already
/// wiped (server URL and branding are kept).
class AccessRemovedScreen extends ConsumerWidget {
  const AccessRemovedScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final theme = Theme.of(context);
    return Scaffold(
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(24),
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 420),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  const BrandHeader(logoSize: 56),
                  const SizedBox(height: 32),
                  Icon(Icons.block, size: 56, color: theme.colorScheme.error),
                  const SizedBox(height: 16),
                  Semantics(
                    liveRegion: true,
                    child: Text(
                      'Your access has been removed.',
                      textAlign: TextAlign.center,
                      style: theme.textTheme.titleLarge,
                    ),
                  ),
                  const SizedBox(height: 8),
                  const Text(
                    'Contact HR. The data stored on this device has been deleted.',
                    textAlign: TextAlign.center,
                  ),
                  const SizedBox(height: 32),
                  FilledButton(
                    onPressed: () => ref.read(authControllerProvider.notifier).bootstrap(),
                    child: const Text('Sign in with another account'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

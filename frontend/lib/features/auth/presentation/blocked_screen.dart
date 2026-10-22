import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/widgets/state_views.dart';
import 'auth_controller.dart';

class BlockedScreen extends ConsumerWidget {
  const BlockedScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) => Scaffold(
        body: SafeArea(
          child: ErrorView(
            message: 'Administrator access is not allowed on a rooted or '
                'jailbroken device.',
            onRetry: () => ref.read(authControllerProvider.notifier).bootstrap(),
          ),
        ),
      );
}

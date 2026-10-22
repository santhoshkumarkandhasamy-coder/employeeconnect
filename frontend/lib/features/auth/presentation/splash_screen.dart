import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/config/constants.dart';
import '../../../core/widgets/state_views.dart';
import 'auth_controller.dart';
import 'auth_state.dart';

class SplashScreen extends ConsumerWidget {
  const SplashScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final error = ref.watch(
      authControllerProvider.select((s) => s is AuthInitial ? s.error : null),
    );
    return Scaffold(
      body: error != null
          ? ErrorView(
              message: error,
              onRetry: () => ref.read(authControllerProvider.notifier).bootstrap(),
            )
          : Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    AppConstants.appName,
                    style: Theme.of(context).textTheme.headlineSmall,
                  ),
                  const SizedBox(height: 24),
                  const LoadingView(),
                ],
              ),
            ),
    );
  }
}

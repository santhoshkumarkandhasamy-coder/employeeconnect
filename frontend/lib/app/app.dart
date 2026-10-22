import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/cache/json_cache.dart';
import '../core/config/constants.dart';
import '../core/security/inactivity_watcher.dart';
import '../core/theme/app_theme.dart';
import '../features/food/data/food_repository.dart' show exportStoreProvider;
import 'router.dart';

class EmployeeConnectApp extends ConsumerWidget {
  const EmployeeConnectApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    // Create the on-disk stores eagerly: sweeps leftover export files from an
    // earlier session and keeps the logout / lock wipe listeners active.
    ref
      ..watch(exportStoreProvider)
      ..watch(jsonCacheProvider);
    return MaterialApp.router(
      title: AppConstants.appName,
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light,
      darkTheme: AppTheme.dark,
      themeMode: ThemeMode.system,
      routerConfig: ref.watch(routerProvider),
      builder: (context, child) =>
          InactivityWatcher(child: child ?? const SizedBox.shrink()),
    );
  }
}

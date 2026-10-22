import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/api/api_exception.dart';
import '../core/server/branding.dart';
import '../core/server/company.dart';
import '../core/theme/app_theme.dart';
import '../core/utils/validators.dart';
import '../core/widgets/password_field.dart';
import '../features/admin/presentation/company_screen.dart' show CompanySetupView;
import 'console_api.dart';
import 'console_auth.dart';
import 'console_shell.dart';

class ConsoleApp extends ConsumerWidget {
  const ConsoleApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final brand = ref.watch(brandingProvider);
    return MaterialApp(
      onGenerateTitle: (_) => '${brandName(brand)} - Admin console',
      debugShowCheckedModeBanner: false,
      theme: AppTheme.light,
      darkTheme: AppTheme.dark,
      home: const ConsoleGate(),
    );
  }
}

/// Chooses login / password change / company setup / shell from the session.
class ConsoleGate extends ConsumerWidget {
  const ConsoleGate({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final s = ref.watch(consoleAuthProvider);
    return switch (s) {
      ConsoleBooting() => const Scaffold(body: Center(child: CircularProgressIndicator())),
      ConsoleSignedOut(:final message) => ConsoleLoginPage(notice: message),
      ConsoleMustChangePassword(:final user) =>
        ConsoleChangePasswordPage(employeeId: user.employeeId, fullName: user.fullName),
      ConsoleSignedIn(:final user) =>
        user.needsCompanySetup
            ? Scaffold(
                body: CompanySetupView(
                  onConfigured: (CompanyInfo c) =>
                      ref.read(consoleAuthProvider.notifier).companyConfigured(c),
                ),
              )
            : const ConsoleInactivityWatcher(child: ConsoleShell()),
    };
  }
}

/// Signs out after `console.inactivity_timeout_minutes` without input.
class ConsoleInactivityWatcher extends ConsumerStatefulWidget {
  const ConsoleInactivityWatcher({
    required this.child,
    super.key,
    this.checkEvery,
    this.clock,
  });
  final Widget child;
  final DateTime Function()? clock;
  final Duration? checkEvery;

  @override
  ConsumerState<ConsoleInactivityWatcher> createState() => _WatcherState();
}

class _WatcherState extends ConsumerState<ConsoleInactivityWatcher> {
  late DateTime _last = _now();
  Timer? _timer;

  DateTime _now() => (widget.clock ?? DateTime.now)();

  @override
  void initState() {
    super.initState();
    _timer = Timer.periodic(widget.checkEvery ?? const Duration(seconds: 20), (_) {
      final timeout = Duration(minutes: ref.read(consoleInactivityMinutesProvider));
      if (consoleIdle(_last, _now(), timeout)) {
        _timer?.cancel();
        unawaited(ref
            .read(consoleAuthProvider.notifier)
            .logout(message: 'You were signed out after a period of inactivity.'),);
      }
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  void _touch([Object? _]) => _last = _now();

  @override
  Widget build(BuildContext context) => Listener(
        behavior: HitTestBehavior.translucent,
        onPointerDown: _touch,
        onPointerMove: _touch,
        onPointerHover: _touch,
        onPointerSignal: _touch,
        child: Focus(
          canRequestFocus: false,
          skipTraversal: true,
          onKeyEvent: (_, __) {
            _touch();
            return KeyEventResult.ignored;
          },
          child: widget.child,
        ),
      );
}

class ConsoleLoginPage extends ConsumerStatefulWidget {
  const ConsoleLoginPage({super.key, this.notice});
  final String? notice;

  @override
  ConsumerState<ConsoleLoginPage> createState() => _LoginState();
}

class _LoginState extends ConsumerState<ConsoleLoginPage> {
  final _key = GlobalKey<FormState>();
  final _id = TextEditingController();
  final _pw = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    unawaited(_loadBranding());
  }

  Future<void> _loadBranding() async {
    try {
      final res = await ref.read(consoleBrandingFetcher)();
      if (res != null && mounted) await ref.read(brandingProvider.notifier).update(res);
    } on Object {
      // cosmetic
    }
  }

  @override
  void dispose() {
    _id.dispose();
    _pw.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_busy || !(_key.currentState?.validate() ?? false)) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(consoleAuthProvider.notifier).login(_id.text, _pw.text);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final msg = _error ?? widget.notice;
    return Scaffold(
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 400),
            child: Form(
              key: _key,
              child: AutofillGroup(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const BrandHeader(),
                    const SizedBox(height: 4),
                    Text('Admin console', textAlign: TextAlign.center, style: theme.textTheme.titleMedium),
                    const SizedBox(height: 24),
                    TextFormField(
                      controller: _id,
                      enabled: !_busy,
                      autocorrect: false,
                      textCapitalization: TextCapitalization.characters,
                      autofillHints: const [AutofillHints.username],
                      textInputAction: TextInputAction.next,
                      validator: validateEmployeeId,
                      decoration: const InputDecoration(
                        labelText: 'Employee ID',
                        prefixIcon: Icon(Icons.badge_outlined),
                      ),
                    ),
                    const SizedBox(height: 16),
                    PasswordField(
                      controller: _pw,
                      label: 'Password',
                      enabled: !_busy,
                      textInputAction: TextInputAction.done,
                      onSubmitted: (_) => _submit(),
                      validator: (v) => validateRequired(v, 'Enter your password'),
                    ),
                    if (msg != null) ...[
                      const SizedBox(height: 16),
                      Semantics(
                        liveRegion: true,
                        child: Text(
                          msg,
                          textAlign: TextAlign.center,
                          style: TextStyle(color: theme.colorScheme.error),
                        ),
                      ),
                    ],
                    const SizedBox(height: 24),
                    FilledButton(
                      onPressed: _busy ? null : _submit,
                      child: _busy
                          ? const SizedBox(
                              height: 22,
                              width: 22,
                              child: CircularProgressIndicator(strokeWidth: 2.5),
                            )
                          : const Text('Sign in'),
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

class ConsoleChangePasswordPage extends ConsumerStatefulWidget {
  const ConsoleChangePasswordPage({required this.employeeId, required this.fullName, super.key});
  final String employeeId;
  final String fullName;

  @override
  ConsumerState<ConsoleChangePasswordPage> createState() => _ChangeState();
}

class _ChangeState extends ConsumerState<ConsoleChangePasswordPage> {
  final _key = GlobalKey<FormState>();
  final _cur = TextEditingController();
  final _new = TextEditingController();
  final _confirm = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _cur.dispose();
    _new.dispose();
    _confirm.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_busy || !(_key.currentState?.validate() ?? false)) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(consoleAuthProvider.notifier).changePassword(_cur.text, _new.text);
    } on ApiException catch (e) {
      if (mounted) setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      body: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 400),
            child: Form(
              key: _key,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text('Choose a new password',
                      textAlign: TextAlign.center, style: theme.textTheme.headlineSmall,),
                  const SizedBox(height: 16),
                  PasswordField(
                    controller: _cur,
                    label: 'Current (temporary) password',
                    validator: (v) => validateRequired(v, 'Enter your current password'),
                  ),
                  const SizedBox(height: 12),
                  PasswordField(
                    controller: _new,
                    label: 'New password',
                    autofillHints: const [AutofillHints.newPassword],
                    validator: (v) => passwordIsValid(
                      v ?? '',
                      employeeId: widget.employeeId,
                      fullName: widget.fullName,
                      current: _cur.text,
                    )
                        ? null
                        : 'Use 8+ characters with upper, lower, number and symbol',
                  ),
                  const SizedBox(height: 12),
                  PasswordField(
                    controller: _confirm,
                    label: 'Confirm new password',
                    onSubmitted: (_) => _submit(),
                    validator: (v) => v == _new.text ? null : 'Passwords do not match',
                  ),
                  if (_error != null) ...[
                    const SizedBox(height: 12),
                    Text(_error!,
                        textAlign: TextAlign.center,
                        style: TextStyle(color: theme.colorScheme.error),),
                  ],
                  const SizedBox(height: 20),
                  FilledButton(
                    onPressed: _busy ? null : _submit,
                    child: const Text('Change password'),
                  ),
                  TextButton(
                    onPressed: () => ref.read(consoleAuthProvider.notifier).logout(),
                    child: const Text('Sign out'),
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

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/server/branding.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/validators.dart';
import '../../../core/widgets/password_field.dart';
import 'auth_controller.dart';
import 'auth_state.dart';

class LoginScreen extends ConsumerStatefulWidget {
  const LoginScreen({super.key});

  @override
  ConsumerState<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends ConsumerState<LoginScreen> {
  final _formKey = GlobalKey<FormState>();
  final _id = TextEditingController();
  final _pw = TextEditingController();
  bool _loading = false;
  String? _error;
  DateTime? _blockedUntil;
  String _blockLabel = '';
  Timer? _ticker;

  @override
  void initState() {
    super.initState();
    Future<void>.microtask(() async {
      try {
        await ref.read(authControllerProvider.notifier).refreshPublicBranding();
      } on Object {
        // branding is cosmetic
      }
    });
  }

  @override
  void dispose() {
    _ticker?.cancel();
    _id.dispose();
    _pw.dispose();
    super.dispose();
  }

  bool get _blocked =>
      _blockedUntil != null && _blockedUntil!.isAfter(DateTime.now());

  Future<void> _submit() async {
    if (_loading || _blocked) return;
    if (!(_formKey.currentState?.validate() ?? false)) return;
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      await ref.read(authControllerProvider.notifier).login(_id.text.trim(), _pw.text);
      // Router redirects once the controller state changes.
    } on ApiException catch (e) {
      if (!mounted) return;
      _handleError(e);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _changeServer() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Change server?'),
        content: const Text('All data stored on this device is removed.'),
        actions: [
          TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
          FilledButton(onPressed: () => Navigator.pop(ctx, true), child: const Text('Change')),
        ],
      ),
    );
    if (ok == true && mounted) {
      await ref.read(authControllerProvider.notifier).changeServer();
    }
  }

  void _handleError(ApiException e) {
    DateTime? until;
    var label = 'Try again in';
    if (e.code == 'ACCOUNT_LOCKED') {
      until = e.lockedUntil ?? DateTime.now().add(const Duration(minutes: 15));
      label = 'Account locked. Try again in';
    } else if (e.code == 'RATE_LIMITED' && e.retryAfter != null) {
      until = DateTime.now().add(e.retryAfter!);
      label = 'Too many attempts. Try again in';
    }
    if (until != null) {
      _blockedUntil = until;
      _blockLabel = label;
      _startTicker();
      _error = null;
    } else {
      _error = loginErrorMessage(e);
    }
    setState(() {});
  }

  void _startTicker() {
    _ticker?.cancel();
    _ticker = Timer.periodic(const Duration(seconds: 1), (t) {
      if (!mounted) return t.cancel();
      if (!_blocked) {
        t.cancel();
        setState(() => _blockedUntil = null);
      } else {
        setState(() {}); // only this screen's small tree rebuilds
      }
    });
  }

  String _remaining() {
    final d = _blockedUntil!.difference(DateTime.now());
    final s = d.inSeconds < 0 ? 0 : d.inSeconds;
    final m = (s ~/ 60).toString().padLeft(2, '0');
    final sec = (s % 60).toString().padLeft(2, '0');
    return '$_blockLabel $m:$sec';
  }

  @override
  Widget build(BuildContext context) {
    final notice = ref.watch(
      authControllerProvider.select((s) => s is AuthUnauthenticated ? s.message : null),
    );
    final theme = Theme.of(context);
    final message = _blocked ? _remaining() : (_error ?? notice);
    return SecureScreen(
      child: Scaffold(
        body: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: Form(
                  key: _formKey,
                  child: AutofillGroup(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        const BrandHeader(),
                        const SizedBox(height: 8),
                        Text(
                          'Sign in with your Employee ID',
                          textAlign: TextAlign.center,
                          style: theme.textTheme.bodyMedium,
                        ),
                        const SizedBox(height: 32),
                        TextFormField(
                          controller: _id,
                          enabled: !_loading,
                          textInputAction: TextInputAction.next,
                          textCapitalization: TextCapitalization.characters,
                          autocorrect: false,
                          autofillHints: const [AutofillHints.username],
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
                          enabled: !_loading,
                          textInputAction: TextInputAction.done,
                          onSubmitted: (_) => _submit(),
                          validator: (v) => validateRequired(v, 'Enter your password'),
                        ),
                        if (message != null) ...[
                          const SizedBox(height: 16),
                          Semantics(
                            liveRegion: true,
                            child: Text(
                              message,
                              textAlign: TextAlign.center,
                              style: TextStyle(color: theme.colorScheme.error),
                            ),
                          ),
                        ],
                        const SizedBox(height: 24),
                        FilledButton(
                          onPressed: (_loading || _blocked) ? null : _submit,
                          child: _loading
                              ? const SizedBox(
                                  height: 22,
                                  width: 22,
                                  child: CircularProgressIndicator(strokeWidth: 2.5),
                                )
                              : const Text('Sign in'),
                        ),
                        const SizedBox(height: 12),
                        Text(
                          'Forgot your password? Contact your administrator.',
                          textAlign: TextAlign.center,
                          style: theme.textTheme.bodySmall,
                        ),
                        TextButton(
                          onPressed: _loading ? null : _changeServer,
                          child: const Text('Change server'),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// User-facing text for a failed sign-in that is not a lock or rate limit.
String loginErrorMessage(ApiException e) => e.code == 'TEMP_PASSWORD_EXPIRED'
    ? 'This temporary password has expired. Ask HR or Admin to reset your password again.'
    : e.message;

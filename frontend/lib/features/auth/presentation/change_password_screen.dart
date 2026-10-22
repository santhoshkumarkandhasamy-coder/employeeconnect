import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/utils/validators.dart';
import '../../../core/widgets/password_field.dart';
import 'auth_controller.dart';
import 'auth_state.dart';

class ChangePasswordScreen extends ConsumerStatefulWidget {
  const ChangePasswordScreen({super.key});

  @override
  ConsumerState<ChangePasswordScreen> createState() => _ChangePasswordScreenState();
}

class _ChangePasswordScreenState extends ConsumerState<ChangePasswordScreen> {
  final _formKey = GlobalKey<FormState>();
  final _current = TextEditingController();
  final _new = TextEditingController();
  final _confirm = TextEditingController();
  bool _loading = false;
  String? _error;
  String? _currentError;

  @override
  void dispose() {
    _current.dispose();
    _new.dispose();
    _confirm.dispose();
    super.dispose();
  }

  String? _employeeId() {
    final s = ref.read(authControllerProvider);
    return s is AuthNeedsPasswordChange ? s.user.employeeId : null;
  }

  String? _fullName() {
    final s = ref.read(authControllerProvider);
    return s is AuthNeedsPasswordChange ? s.user.fullName : null;
  }

  Future<void> _submit() async {
    if (_loading) return;
    setState(() {
      _error = null;
      _currentError = null;
    });
    if (!(_formKey.currentState?.validate() ?? false)) return;
    setState(() => _loading = true);
    try {
      await ref
          .read(authControllerProvider.notifier)
          .changePassword(_current.text, _new.text);
    } on ApiException catch (e) {
      if (!mounted) return;
      setState(() {
        if (e.code == 'CURRENT_PASSWORD_INVALID') {
          _currentError = 'Current password is wrong';
        } else if (e.code == 'ACCOUNT_LOCKED') {
          final until = e.lockedUntil;
          _error = until == null
              ? 'Too many wrong attempts. Your account is locked.'
              : 'Too many wrong attempts. Your account is locked until '
                  '${until.hour.toString().padLeft(2, '0')}:'
                  '${until.minute.toString().padLeft(2, '0')}.';
        } else if (e.code == 'VALIDATION_ERROR' && e.details.isNotEmpty) {
          _error = e.details.map((d) => d.message).join('\n');
        } else if (e.code == 'PASSWORD_REUSED') {
          _error = 'Choose a password different from your current one.';
        } else {
          _error = e.message;
        }
      });
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return SecureScreen(
      child: Scaffold(
        appBar: AppBar(
          title: const Text('Change password'),
          automaticallyImplyLeading: false,
          actions: [
            TextButton(
              onPressed: _loading
                  ? null
                  : () => ref.read(authControllerProvider.notifier).logout(),
              child: const Text('Log out'),
            ),
          ],
        ),
        body: SafeArea(
          child: Center(
            child: SingleChildScrollView(
              padding: const EdgeInsets.all(24),
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 420),
                child: Form(
                  key: _formKey,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text(
                        'You must set a new password before continuing.',
                        style: theme.textTheme.bodyLarge,
                      ),
                      const SizedBox(height: 24),
                      PasswordField(
                        controller: _current,
                        label: 'Current password',
                        enabled: !_loading,
                        errorText: _currentError,
                        textInputAction: TextInputAction.next,
                        validator: (v) => validateRequired(v, 'Enter your current password'),
                      ),
                      const SizedBox(height: 16),
                      PasswordField(
                        controller: _new,
                        label: 'New password',
                        enabled: !_loading,
                        autofillHints: const [AutofillHints.newPassword],
                        textInputAction: TextInputAction.next,
                        validator: (v) => passwordIsValid(
                          v ?? '',
                          employeeId: _employeeId(),
                          fullName: _fullName(),
                          current: _current.text,
                        )
                            ? null
                            : 'Password does not meet the rules below',
                      ),
                      const SizedBox(height: 8),
                      // Only the checklist rebuilds while typing.
                      ListenableBuilder(
                        listenable: Listenable.merge([_new, _current]),
                        builder: (context, _) => _Checklist(
                          results: evaluatePassword(
                            _new.text,
                            employeeId: _employeeId(),
                            fullName: _fullName(),
                            current: _current.text,
                          ),
                          started: _new.text.isNotEmpty,
                        ),
                      ),
                      const SizedBox(height: 16),
                      PasswordField(
                        controller: _confirm,
                        label: 'Confirm new password',
                        enabled: !_loading,
                        autofillHints: const [AutofillHints.newPassword],
                        textInputAction: TextInputAction.done,
                        onSubmitted: (_) => _submit(),
                        validator: (v) =>
                            v == _new.text ? null : 'Passwords do not match',
                      ),
                      if (_error != null) ...[
                        const SizedBox(height: 16),
                        Semantics(
                          liveRegion: true,
                          child: Text(
                            _error!,
                            style: TextStyle(color: theme.colorScheme.error),
                          ),
                        ),
                      ],
                      const SizedBox(height: 24),
                      FilledButton(
                        onPressed: _loading ? null : _submit,
                        child: _loading
                            ? const SizedBox(
                                height: 22,
                                width: 22,
                                child: CircularProgressIndicator(strokeWidth: 2.5),
                              )
                            : const Text('Update password'),
                      ),
                    ],
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

class _Checklist extends StatelessWidget {
  const _Checklist({required this.results, required this.started});
  final List<PasswordRuleResult> results;
  final bool started;

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final r in results)
          Semantics(
            label: '${r.label}: ${r.passed ? 'met' : 'not met'}',
            excludeSemantics: true,
            child: Padding(
              padding: const EdgeInsets.symmetric(vertical: 2),
              child: Row(
                children: [
                  Icon(
                    r.passed ? Icons.check_circle : Icons.circle_outlined,
                    size: 16,
                    color: r.passed
                        ? Colors.green
                        : (started ? scheme.error : scheme.outline),
                  ),
                  const SizedBox(width: 8),
                  Expanded(child: Text(r.label, style: Theme.of(context).textTheme.bodySmall)),
                ],
              ),
            ),
          ),
      ],
    );
  }
}

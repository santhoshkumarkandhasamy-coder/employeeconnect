import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/security/screen_protect.dart';
import '../../../core/widgets/state_views.dart';
import '../../announcements/presentation/announcement_providers.dart' show PagedList;
import '../../auth/presentation/auth_controller.dart';
import '../../auth/presentation/auth_state.dart';
import '../domain/ops_models.dart';
import 'admin_providers.dart';

/// Permissions of the signed-in user (empty when signed out).
final adminPermissionsProvider = Provider<List<String>>((ref) {
  final s = ref.watch(authControllerProvider);
  return s is AuthAuthenticated ? s.user.permissions : const <String>[];
});

/// The signed-in user's id (to hide self-deactivate / self-delete).
final adminSelfIdProvider = Provider<String?>((ref) {
  final s = ref.watch(authControllerProvider);
  return s is AuthAuthenticated ? s.user.id : null;
});

/// Shown instead of a screen when the user lacks its permission (the server
/// enforces the permission anyway). Screens check the permission BEFORE they
/// watch any data provider so no request is made.
class NoPermissionScreen extends StatelessWidget {
  const NoPermissionScreen({required this.title, super.key});
  final String title;

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: Text(title)),
        body: const EmptyView(
          icon: Icons.lock_outline,
          message: 'You do not have permission to view this.',
        ),
      );
}

Future<bool> confirmAction(
  BuildContext context, {
  required String title,
  required String message,
  required String confirmLabel,
}) async {
  final ok = await showDialog<bool>(
    context: context,
    builder: (ctx) => AlertDialog(
      title: Text(title),
      content: Text(message),
      actions: [
        TextButton(onPressed: () => Navigator.pop(ctx, false), child: const Text('Cancel')),
        FilledButton(
          onPressed: () => Navigator.pop(ctx, true),
          style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
          child: Text(confirmLabel),
        ),
      ],
    ),
  );
  return ok ?? false;
}

void showAdminSnack(BuildContext context, String message) {
  ScaffoldMessenger.of(context)
    ..hideCurrentSnackBar()
    ..showSnackBar(SnackBar(content: Text(message)));
}

/// Clears the clipboard only if it still holds [value] (so something the user
/// copied afterwards is left alone). Best effort; never throws.
Future<void> clearClipboardIfHolds(String value) async {
  try {
    final data = await Clipboard.getData(Clipboard.kTextPlain);
    if (data?.text == value) {
      await Clipboard.setData(const ClipboardData(text: ''));
    }
  } on Object {
    // clipboard unavailable: nothing to clear
  }
}

/// One-time temporary password. FLAG_SECURE is on while it is visible. If the
/// user copies it, the clipboard is cleared after [clipboardTtl] (60 s) and
/// when the dialog closes, but only while it still holds the password (L8).
///
/// Not done: marking the clip as sensitive (Android `EXTRA_IS_SENSITIVE`,
/// which hides the clipboard preview) needs a native platform channel, which
/// this change deliberately avoids.
Future<void> showTempPasswordDialog(
  BuildContext context, {
  required String employeeLabel,
  required String password,
  Duration clipboardTtl = const Duration(seconds: 60),
}) =>
    showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (_) => _TempPasswordDialog(
        employeeLabel: employeeLabel,
        password: password,
        clipboardTtl: clipboardTtl,
      ),
    );

class _TempPasswordDialog extends StatefulWidget {
  const _TempPasswordDialog({
    required this.employeeLabel,
    required this.password,
    required this.clipboardTtl,
  });
  final String employeeLabel;
  final String password;
  final Duration clipboardTtl;

  @override
  State<_TempPasswordDialog> createState() => _TempPasswordDialogState();
}

class _TempPasswordDialogState extends State<_TempPasswordDialog> {
  Timer? _timer;
  bool _copied = false;

  @override
  void dispose() {
    _timer?.cancel();
    if (_copied) unawaited(clearClipboardIfHolds(widget.password));
    super.dispose();
  }

  Future<void> _copy() async {
    await Clipboard.setData(ClipboardData(text: widget.password));
    _copied = true;
    _timer?.cancel();
    _timer = Timer(widget.clipboardTtl, () {
      _copied = false;
      unawaited(clearClipboardIfHolds(widget.password));
    });
    if (mounted) showAdminSnack(context, 'Copied. The clipboard is cleared in 60 seconds.');
  }

  @override
  Widget build(BuildContext context) {
    return SecureScreen(
      child: AlertDialog(
        title: const Text('Temporary password'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('For ${widget.employeeLabel}. It is shown only once. The employee must '
                'change it at the next sign-in. It expires after a limited time '
                '(72 hours by default); after that an administrator must reset it again.'),
            const SizedBox(height: 16),
            Container(
              width: double.infinity,
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: Theme.of(context).colorScheme.surfaceContainerHighest,
                borderRadius: BorderRadius.circular(8),
              ),
              child: SelectableText(
                widget.password,
                key: const ValueKey('temp-password'),
                style: const TextStyle(fontFamily: 'monospace', fontSize: 18),
              ),
            ),
          ],
        ),
        actions: [
          TextButton.icon(
            onPressed: _copy,
            icon: const Icon(Icons.copy),
            label: const Text('Copy'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context),
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Done'),
          ),
        ],
      ),
    );
  }
}

/// Asks the signed-in user for their own current password (step-up
/// re-authentication, review M1). Returns the text or null when cancelled. The
/// value is never stored or logged; the controller is disposed with the dialog.
Future<String?> showPasswordConfirmDialog(
  BuildContext context, {
  required String reason,
  String? errorText,
}) =>
    showDialog<String>(
      context: context,
      barrierDismissible: false,
      builder: (_) => _PasswordConfirmDialog(reason: reason, errorText: errorText),
    );

class _PasswordConfirmDialog extends StatefulWidget {
  const _PasswordConfirmDialog({required this.reason, this.errorText});
  final String reason;
  final String? errorText;

  @override
  State<_PasswordConfirmDialog> createState() => _PasswordConfirmDialogState();
}

class _PasswordConfirmDialogState extends State<_PasswordConfirmDialog> {
  final _form = GlobalKey<FormState>();
  final _pw = TextEditingController();

  @override
  void dispose() {
    _pw.dispose();
    super.dispose();
  }

  void _submit() {
    if (_form.currentState?.validate() ?? false) Navigator.pop(context, _pw.text);
  }

  @override
  Widget build(BuildContext context) {
    return SecureScreen(
      child: AlertDialog(
        title: const Text('Confirm your password'),
        content: Form(
          key: _form,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(widget.reason),
              const SizedBox(height: 12),
              TextFormField(
                key: const ValueKey('reauth-password'),
                controller: _pw,
                obscureText: true,
                autofocus: true,
                enableSuggestions: false,
                autocorrect: false,
                keyboardType: TextInputType.visiblePassword,
                textInputAction: TextInputAction.done,
                onFieldSubmitted: (_) => _submit(),
                decoration: InputDecoration(
                  labelText: 'Your current password',
                  errorText: widget.errorText,
                  border: const OutlineInputBorder(),
                ),
                validator: (v) => (v == null || v.isEmpty) ? 'Enter your password' : null,
              ),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: _submit,
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Confirm'),
          ),
        ],
      ),
    );
  }
}

/// Prompts for the user's password and runs [action] with it. A wrong password
/// (403 `REAUTH_FAILED`) re-prompts, up to [maxAttempts] times; any other
/// [ApiException] is rethrown for the caller. Returns false when cancelled.
Future<bool> runWithReauth(
  BuildContext context, {
  required String reason,
  required Future<void> Function(String currentPassword) action,
  int maxAttempts = 3,
}) async {
  String? error;
  for (var i = 0; i < maxAttempts; i++) {
    if (!context.mounted) return false;
    final pw = await showPasswordConfirmDialog(context, reason: reason, errorText: error);
    if (pw == null || !context.mounted) return false;
    try {
      await action(pw);
      return true;
    } on ApiException catch (e) {
      if (e.code != 'REAUTH_FAILED') rethrow;
      error = 'That password is not correct.';
    }
  }
  throw const ApiException(
    code: 'REAUTH_FAILED',
    message: 'Your password was not correct.',
  );
}

/// Runs a password reset. The server only wants the actor's password when the
/// target holds a permission (contract section 9), so unless [needsPassword]
/// is known to be true the call is made without it first; a 422 naming
/// `current_password` then falls back to the prompt. Returns the temporary
/// password, or null when the user cancelled.
Future<String?> resetWithStepUp(
  BuildContext context, {
  required bool needsPassword,
  required Future<String> Function(String? currentPassword) call,
}) async {
  if (!needsPassword) {
    try {
      return await call(null);
    } on ApiException catch (e) {
      if (e.code != 'VALIDATION_ERROR' || e.detail('current_password') == null) rethrow;
    }
  }
  if (!context.mounted) return null;
  String? temp;
  final done = await runWithReauth(
    context,
    reason: 'Resetting the password of this account needs your own password.',
    action: (current) async {
      temp = await call(current);
    },
  );
  return done ? temp : null;
}

/// Loading / empty / error / paged list body shared by admin lists.
class AdminPagedBody<T> extends StatefulWidget {
  const AdminPagedBody({
    required this.async,
    required this.itemBuilder,
    required this.onRefresh,
    required this.onLoadMore,
    required this.emptyMessage,
    super.key,
    this.emptyIcon = Icons.inbox_outlined,
    this.header,
  });
  final AsyncValue<PagedList<T>> async;
  final Widget Function(BuildContext, T) itemBuilder;
  final Future<void> Function() onRefresh;
  final VoidCallback onLoadMore;
  final String emptyMessage;
  final IconData emptyIcon;
  final Widget? header;

  @override
  State<AdminPagedBody<T>> createState() => _AdminPagedBodyState<T>();
}

class _AdminPagedBodyState<T> extends State<AdminPagedBody<T>> {
  final _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.hasClients && _scroll.position.extentAfter < 300) widget.onLoadMore();
    });
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        if (widget.header != null) widget.header!,
        Expanded(
          child: widget.async.when(
            skipLoadingOnReload: true,
            loading: () => const LoadingView(),
            error: (e, _) => ErrorView(message: adminMsg(e), onRetry: widget.onRefresh),
            data: (s) => RefreshIndicator(
              onRefresh: widget.onRefresh,
              child: s.items.isEmpty
                  ? ListView(
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: [
                        const SizedBox(height: 80),
                        EmptyView(icon: widget.emptyIcon, message: widget.emptyMessage),
                      ],
                    )
                  : ListView.builder(
                      controller: _scroll,
                      physics: const AlwaysScrollableScrollPhysics(),
                      padding: const EdgeInsets.fromLTRB(12, 8, 12, 96),
                      itemCount: s.items.length + 1,
                      itemBuilder: (context, i) {
                        if (i == s.items.length) {
                          if (s.loadingMore) {
                            return const Padding(
                              padding: EdgeInsets.all(16),
                              child: Center(child: CircularProgressIndicator()),
                            );
                          }
                          if (s.loadMoreError != null) {
                            return TextButton(
                              onPressed: widget.onLoadMore,
                              child: Text('${s.loadMoreError} Try again'),
                            );
                          }
                          return const SizedBox.shrink();
                        }
                        return widget.itemBuilder(context, s.items[i]);
                      },
                    ),
            ),
          ),
        ),
      ],
    );
  }
}

/// Dropdown of [NamedRef]-like options with validation.
class RefDropdown extends StatelessWidget {
  const RefDropdown({
    required this.label,
    required this.options,
    required this.value,
    required this.onChanged,
    super.key,
    this.errorText,
  });
  final String label;
  final List<({String id, String name})> options;
  final String? value;
  final ValueChanged<String?> onChanged;
  final String? errorText;

  @override
  Widget build(BuildContext context) {
    final valid = options.any((o) => o.id == value) ? value : null;
    return DropdownButtonFormField<String>(
      initialValue: valid,
      isExpanded: true,
      decoration: InputDecoration(labelText: label, border: const OutlineInputBorder()),
      items: [
        for (final o in options)
          DropdownMenuItem(value: o.id, child: Text(o.name, overflow: TextOverflow.ellipsis)),
      ],
      onChanged: onChanged,
      validator: (v) => v == null ? 'Choose ${label.toLowerCase()}' : null,
    );
  }
}

/// Offboarding dialog: pick the reason, then confirm. Null when cancelled.
Future<DeactivationReason?> pickDeactivationReason(BuildContext context, String name) =>
    showDialog<DeactivationReason>(
      context: context,
      builder: (_) => _ReasonDialog(name: name),
    );

class _ReasonDialog extends StatefulWidget {
  const _ReasonDialog({required this.name});
  final String name;

  @override
  State<_ReasonDialog> createState() => _ReasonDialogState();
}

class _ReasonDialogState extends State<_ReasonDialog> {
  DeactivationReason _reason = DeactivationReason.resigned;

  @override
  Widget build(BuildContext context) => AlertDialog(
        title: Text('Deactivate ${widget.name}?'),
        content: SingleChildScrollView(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'They are signed out of every device, local app data is removed at their '
                'next contact with the server, and pending requests are cancelled.',
              ),
              const SizedBox(height: 8),
              RadioGroup<DeactivationReason>(
                groupValue: _reason,
                onChanged: (v) => setState(() => _reason = v ?? _reason),
                child: Column(
                  children: [
                    for (final r in DeactivationReason.values)
                      RadioListTile<DeactivationReason>(
                        dense: true,
                        value: r,
                        title: Text(r.label),
                      ),
                  ],
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(onPressed: () => Navigator.pop(context), child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(context, _reason),
            style: FilledButton.styleFrom(minimumSize: const Size(96, 44)),
            child: const Text('Deactivate'),
          ),
        ],
      );
}

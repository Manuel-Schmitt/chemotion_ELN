# frozen_string_literal: true

# Registers a synchronous LDAP/AD membership check as a before_create hook, from the outside,
# without touching app/models/user.rb.
#
# The LDAP connection itself is handled by LdapMembershipCheck (see
# config/initializers/ldap_membership_check.rb). All LDAP settings are read from the
# environment (.env file); leave LDAP_HOST/LDAP_BASE/LDAP_ACTIVATION_GROUP_DN blank to disable
# this hook. The feature also needs DEVISE_NEW_ACCOUNT_INACTIVE=true to be set in the
# environment, so new Person accounts start out disabled pending this check.

Rails.application.config.to_prepare do
  next if User._create_callbacks.any? { |cb| cb.kind == :before && cb.filter == :ldap_activation_check }

  User.before_create(
    :ldap_activation_check,
    if: proc { |user|
      user.type == 'Person' && !user.account_active? && LdapMembershipCheck.enabled? &&
        ENV['LDAP_ACTIVATION_GROUP_DN'].present?
    },
  )

  User.send(:define_method, :ldap_activation_check) do
    account_attribute = ENV['LDAP_ACCOUNT_ATTRIBUTE'].presence || 'name_abbreviation'
    uid = public_send(account_attribute)

    if LdapMembershipCheck.member?(ENV['LDAP_ACTIVATION_GROUP_DN'], uid)
      self.account_active = true
    else
      errors.add(:base, 'is not a member of the required LDAP group')
      throw :abort
    end
  rescue Net::LDAP::Error => e
    # Block registration rather than silently creating a disabled account we can't verify.
    Rails.logger.error("LDAP group membership lookup failed for #{uid}: #{e.message}")
    errors.add(:base, 'could not be verified against the LDAP directory, please try again later')
    throw :abort
  end
  User.send(:private, :ldap_activation_check)
end

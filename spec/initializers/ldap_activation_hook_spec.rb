# frozen_string_literal: true

require 'rails_helper'

# Verifies the before_create hook registered from
# config/initializers/ldap_activation_hook.rb (not defined in app/models/user.rb) runs
# synchronously right before a disabled Person account is persisted.
# rubocop:disable RSpec/DescribeClass
describe 'LDAP activation hook' do
  # Person#account_active is forced by User#set_account_active based on this env var, so it
  # has to be set to actually get a disabled account out of the factory.
  def build_disabled_person(**attrs)
    stub_const('ENV', ENV.to_hash.merge('DEVISE_NEW_ACCOUNT_INACTIVE' => 'true'))
    build(:person, **attrs)
  end

  context 'when LDAP is not configured' do
    before { allow(LdapMembershipCheck).to receive(:enabled?).and_return(false) }

    it 'creates the user and leaves the account disabled' do
      user = build_disabled_person

      user.save!

      expect(user.reload.account_active).to be(false)
    end
  end

  context 'when the account is already active' do
    before do
      stub_const('ENV', ENV.to_hash.merge('LDAP_ACTIVATION_GROUP_DN' => 'cn=eln-users,ou=groups,dc=example,dc=org'))
      allow(LdapMembershipCheck).to receive(:enabled?).and_return(true)
    end

    it 'does not run the LDAP check' do
      user = build(:person)
      allow(LdapMembershipCheck).to receive(:member?)

      user.save!

      expect(LdapMembershipCheck).not_to have_received(:member?)
    end
  end

  context 'when LDAP is configured' do
    before do
      stub_const('ENV', ENV.to_hash.merge('LDAP_ACTIVATION_GROUP_DN' => 'cn=eln-users,ou=groups,dc=example,dc=org'))
      allow(LdapMembershipCheck).to receive(:enabled?).and_return(true)
    end

    it 'activates and persists the user when they are a member of the configured group' do
      user = build_disabled_person(name_abbreviation: 'jd')
      allow(LdapMembershipCheck).to receive(:member?)
        .with('cn=eln-users,ou=groups,dc=example,dc=org', 'jd').and_return(true)

      user.save!

      expect(user.reload.account_active).to be(true)
    end

    it 'rejects (never persists) a user who is not a member of the configured group' do
      user = build_disabled_person(name_abbreviation: 'jd')
      allow(LdapMembershipCheck).to receive(:member?)
        .with('cn=eln-users,ou=groups,dc=example,dc=org', 'jd').and_return(false)

      expect { user.save! }.to raise_error(ActiveRecord::RecordNotSaved)
      expect(User.unscoped.where(name_abbreviation: 'jd')).not_to exist
    end

    it 'rejects (never persists) a user when the LDAP connection raises' do
      user = build_disabled_person(name_abbreviation: 'jd')
      allow(LdapMembershipCheck).to receive(:member?)
        .with('cn=eln-users,ou=groups,dc=example,dc=org', 'jd')
        .and_raise(Net::LDAP::Error, 'connection refused')

      expect { user.save! }.to raise_error(ActiveRecord::RecordNotSaved)
      expect(User.unscoped.where(name_abbreviation: 'jd')).not_to exist
    end
  end
end
# rubocop:enable RSpec/DescribeClass

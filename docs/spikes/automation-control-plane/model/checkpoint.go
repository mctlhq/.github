package model

import "sync"

// Authority stands in for the existing server-side authority (tenant
// membership, install revisions, SurfaceIdentityLink). The model never grants:
// it only answers "is this still allowed now".
type Authority struct {
	mu         sync.Mutex
	members    map[string]bool // tenant|owner
	revoked    map[string]bool // install
	links      map[string]bool // owner|surface verified SurfaceIdentityLink
	InstallRev map[string]int
}

func NewAuthority() *Authority {
	return &Authority{members: map[string]bool{}, revoked: map[string]bool{}, links: map[string]bool{}, InstallRev: map[string]int{}}
}
func (a *Authority) AddMember(tenant, owner string) {
	a.mu.Lock()
	a.members[tenant+"|"+owner] = true
	a.mu.Unlock()
}
func (a *Authority) RemoveMember(tenant, owner string) {
	a.mu.Lock()
	delete(a.members, tenant+"|"+owner)
	a.mu.Unlock()
}
func (a *Authority) Revoke(install string) { a.mu.Lock(); a.revoked[install] = true; a.mu.Unlock() }
func (a *Authority) Link(owner, surface string) {
	a.mu.Lock()
	a.links[owner+"|"+surface] = true
	a.mu.Unlock()
}
func (a *Authority) Unlink(owner, surface string) {
	a.mu.Lock()
	delete(a.links, owner+"|"+surface)
	a.mu.Unlock()
}
func (a *Authority) Linked(owner, surface string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.links[owner+"|"+surface]
}

// Check is the fire-time AND action-time checkpoint (same server-side answer).
func (a *Authority) Check(d *Definition) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.members[d.Tenant+"|"+d.Owner] {
		return ErrOwnerNotMember
	}
	if a.revoked[d.Install] || (d.InstallRev != 0 && a.InstallRev[d.Install] != 0 && a.InstallRev[d.Install] != d.InstallRev) {
		return ErrInstallRevoked
	}
	return nil
}
